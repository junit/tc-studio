"""
智能体核心执行引擎：基于 smolagents.CodeAgent 的流式执行与多模型调度
"""

import inspect
import json
import logging
import math
import multiprocessing
import os
import re
import select
import signal
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import ctypes
import weakref
from collections import OrderedDict
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from contextlib import suppress
from functools import wraps
from datetime import UTC, datetime
from typing import Any, cast

import smolagents.agents as saa
from smolagents import AgentMaxStepsError, CodeAgent, OpenAIServerModel
from smolagents.agents import ActionOutput
import smolagents.local_python_executor as local_python_executor
from smolagents.local_python_executor import CodeOutput, PythonExecutor
from smolagents.memory import ActionStep, FinalAnswerStep, PlanningStep, ToolCall
from smolagents.models import (
    ChatMessageStreamDelta,
    ChatMessageToolCallStreamDelta,
    TokenUsage,
)

from ..cloud_client import UniversalCloudClient
from ..runtime_context import runtime_auth_scope, runtime_is_admin, runtime_user_id
from .bot_manager import BotManager, bot_manager
from .constants import (
    AUTHORIZED_IMPORTS,
    DEFAULT_MAX_PRINT_OUTPUTS_LENGTH,
    DEFAULT_STREAM_MAX_STEPS,
    DEFAULT_SUBTASK_MAX_STEPS,
    DEFAULT_WORKING_MEMORY_CHAR_BUDGET,
    DEFAULT_WORKING_MEMORY_TURNS,
    PROVIDER_DEFAULTS,
)
from .file_parser import format_file_size, get_file_category_and_type
from .models import BotConfig, LLMConfig
from .tools import (
    CloudCallTool,
    CloudGetDocTool,
    CloudSearchApisTool,
    build_bot_tools,
    build_sandbox_scope,
)

MAX_GENERATED_FILE_BYTES = 100 * 1024 * 1024
MAX_GENERATED_FILES = 1000
MAX_GENERATED_TOTAL_BYTES = 512 * 1024 * 1024
MAX_SANDBOX_FOR_ITERATIONS = 100_000
DEFAULT_CODE_EXECUTION_TIMEOUT_SECONDS = 30.0
MAX_EXECUTOR_IPC_BYTES = 8 * 1024 * 1024
MAX_EXECUTOR_STATE_BYTES = 512 * 1024

# ``open`` is exposed to model code as a deliberately small file capability.
# Keep the quota check and the write itself in one critical section so two
# executor threads cannot each observe the same remaining space and exceed the
# bot's aggregate output budget.
_SANDBOX_FILE_QUOTA_LOCK = threading.RLock()
_SANDBOX_FILE_ESCAPE_ATTRIBUTES = frozenset(
    {"buffer", "raw", "detach", "fileno", "__dict__"}
)
_SANDBOX_QUOTA_FILES: weakref.WeakKeyDictionary[Any, dict[str, Any]] = (
    weakref.WeakKeyDictionary()
)
_SANDBOX_QUOTA_TOTALS: dict[str, int] = {}


def _sandbox_quota_file_state(proxy: Any) -> dict[str, Any]:
    try:
        return _SANDBOX_QUOTA_FILES[proxy]
    except KeyError as exc:
        raise ValueError("沙箱文件对象已失效") from exc


def _executor_wire_value(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> Any:
    """Convert executor results/state to bounded JSON values before IPC.

    Values produced by model code must never be unpickled in the parent
    process.  Unknown objects are represented as text, while ordinary JSON
    values retain their useful shape for CodeAgent observations.
    """
    if budget is None:
        budget = [MAX_EXECUTOR_STATE_BYTES]
    if budget[0] <= 0 or depth > 8:
        return "<value truncated>"
    if value is None or isinstance(value, (str, bool, int)):
        text_len = len(value.encode("utf-8")) if isinstance(value, str) else 8
        budget[0] -= text_len
        return value if budget[0] >= 0 else "<value truncated>"
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        budget[0] -= 8
        return value if budget[0] >= 0 else "<value truncated>"
    if isinstance(value, (bytes, bytearray)):
        budget[0] -= min(len(value), 256)
        return f"<bytes:{len(value)}>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 1000:
                break
            if budget[0] <= 0:
                break
            result[str(key)] = _executor_wire_value(item, depth=depth + 1, budget=budget)
        return result
    if isinstance(value, (list, tuple, set)):
        result = []
        for index, item in enumerate(value):
            if index >= 1000:
                break
            if budget[0] <= 0:
                break
            result.append(_executor_wire_value(item, depth=depth + 1, budget=budget))
        return result
    text = str(value)
    budget[0] -= len(text.encode("utf-8"))
    return text if budget[0] >= 0 else "<value truncated>"


def _executor_state_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """Return only bounded, non-callable user variables for parent sync."""
    snapshot: dict[str, Any] = {}
    budget = [MAX_EXECUTOR_STATE_BYTES]
    for key, value in state.items():
        key = str(key)
        if key.startswith("_") or callable(value) or budget[0] <= 0:
            continue
        snapshot[key] = _executor_wire_value(value, budget=budget)
    return snapshot


def _executor_send(conn: Any, message: dict[str, Any]) -> None:
    payload = json.dumps(
        _executor_wire_value(message, budget=[MAX_EXECUTOR_IPC_BYTES]),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(payload) > MAX_EXECUTOR_IPC_BYTES:
        raise RuntimeError("Python 执行器 IPC 消息超过安全上限")
    conn.send_bytes(payload)


def _executor_recv(conn: Any) -> dict[str, Any]:
    payload = conn.recv_bytes(MAX_EXECUTOR_IPC_BYTES)
    message = json.loads(payload.decode("utf-8"))
    if not isinstance(message, dict):
        raise RuntimeError("Python 执行器 IPC 消息格式无效")
    return message


def _terminate_worker_group(_signum: int = signal.SIGTERM, _frame: Any = None) -> None:
    """Terminate the evaluator and every descendant in its private group."""
    if os.name == "posix" and os.getpgrp() == os.getpid():
        with suppress(OSError):
            os.killpg(os.getpgrp(), signal.SIGKILL)
    os._exit(128 + int(_signum))


def _set_linux_parent_death_signal() -> None:
    """Ask Linux to notify the worker when its parent disappears.

    The liveness pipe below is the portable fallback.  ``PR_SET_PDEATHSIG``
    closes the small race where a worker can be orphaned before its watchdog
    has started, and the SIGTERM handler turns that notification into a
    process-group kill so grandchildren cannot remain behind.
    """
    if sys.platform != "linux":
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [ctypes.c_int, ctypes.c_ulong]
        prctl.restype = ctypes.c_int
        if prctl(1, int(signal.SIGTERM)) != 0:  # PR_SET_PDEATHSIG
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
    except Exception:
        # The liveness pipe still protects the normal parent-death path.  Do
        # not make worker startup depend on ctypes/libc details on BSD hosts.
        logging.getLogger(__name__).warning("无法设置 Linux PDEATHSIG", exc_info=True)


def _watch_parent_liveness(liveness_conn: Any) -> None:
    """Kill this worker group when the parent closes its liveness pipe."""
    try:
        liveness_conn.recv_bytes()
    except (EOFError, OSError, BrokenPipeError):
        _terminate_worker_group(signal.SIGTERM)
    except Exception:
        _terminate_worker_group(signal.SIGTERM)


def _windows_assign_kill_on_close_job(pid: int) -> int | None:
    """Put a spawn worker tree in a Windows Job Object.

    Closing the parent's job handle (including process exit) then terminates
    every descendant.  The liveness pipe remains useful as a fallback when a
    host disallows nested jobs.
    """
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong
        ]
        kernel32.SetInformationJobObject.restype = ctypes.c_bool
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_ulong),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_ulong),
                ("Affinity", ctypes.c_void_p),
                ("PriorityClass", ctypes.c_ulong),
                ("SchedulingClass", ctypes.c_ulong),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class _ExtendedLimit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _BasicLimit), ("IoInfo", _IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        process_handle = kernel32.OpenProcess(0x0200 | 0x0001, False, int(pid))
        if not process_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel32.AssignProcessToJobObject(handle, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(process_handle)
        return int(handle)
    except Exception:
        with suppress(Exception):
            kernel32.CloseHandle(handle)
        logging.getLogger(__name__).warning("无法将 Windows executor 放入 kill-on-close Job Object", exc_info=True)
        return None


def _windows_close_job(handle: int | None) -> None:
    if handle and os.name == "nt":
        with suppress(Exception):
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)


def _prepare_worker_lifecycle(
    liveness_conn: Any | None,
    expected_parent_pid: int | None,
    inherited_liveness_writer: Any | None = None,
) -> int | None:
    process_group = None
    if os.name == "posix":
        try:
            os.setsid()
            process_group = os.getpgrp()
            if process_group != os.getpid():
                process_group = None
        except OSError:
            process_group = None
    if inherited_liveness_writer is not None:
        with suppress(Exception):
            inherited_liveness_writer.close()
    signal.signal(signal.SIGTERM, _terminate_worker_group)
    _set_linux_parent_death_signal()
    if expected_parent_pid is not None and os.getppid() != expected_parent_pid:
        _terminate_worker_group(signal.SIGTERM)
    if liveness_conn is not None:
        threading.Thread(
            target=_watch_parent_liveness,
            args=(liveness_conn,),
            name="tim-code-executor-parent-watch",
            daemon=True,
        ).start()
    return process_group


class _ChildRpcProxy:
    """Callable capability proxy used by spawn workers."""

    def __init__(self, conn: Any, name: str):
        self._conn = conn
        self._name = name

    def __getattribute__(self, name: str) -> Any:
        if name.startswith("_") and name != "__class__":
            raise AttributeError(name)
        return object.__getattribute__(self, name)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        conn = object.__getattribute__(self, "_conn")
        name = object.__getattribute__(self, "_name")
        _executor_send(
            conn,
            {
                "kind": "tool_request",
                "name": name,
                "args": list(args),
                "kwargs": kwargs,
            },
        )
        while True:
            if not conn.poll(0.2):
                continue
            try:
                message = _executor_recv(conn)
            except (EOFError, OSError, ValueError, RuntimeError) as exc:
                raise RuntimeError("Python 执行器进程意外退出") from exc
            kind = message.get("kind")
            if kind == "tool_response":
                if message.get("ok"):
                    return message.get("result")
                raise RuntimeError(str(message.get("error") or "工具调用失败"))
            if kind == "stop":
                raise RuntimeError("Python 执行器已停止")


def _spawn_executor_worker(
    conn: Any,
    additional_authorized_imports: list[str],
    max_print_outputs_length: int | None,
    tool_names: list[str],
    function_names: list[str],
    liveness_conn: Any | None,
    inherited_liveness_writer: Any | None,
    expected_parent_pid: int,
) -> None:
    """Spawn-safe evaluator; capabilities are dispatched by the parent."""
    process_group = _prepare_worker_lifecycle(
        liveness_conn, expected_parent_pid, inherited_liveness_writer
    )
    try:
        local = local_python_executor.LocalPythonExecutor(
            additional_authorized_imports,
            max_print_outputs_length=max_print_outputs_length,
            timeout_seconds=None,
        )
        base_tools = getattr(local_python_executor, "BASE_PYTHON_TOOLS", {})
        local.static_tools = {
            **{name: _ChildRpcProxy(conn, name) for name in tool_names},
            **dict(base_tools),
            **{name: _ChildRpcProxy(conn, name) for name in function_names},
        }
        with suppress(Exception):
            _executor_send(conn, {"kind": "ready", "process_group": process_group})
        while True:
            try:
                message = _executor_recv(conn)
            except (EOFError, OSError, ValueError, RuntimeError):
                return
            kind = message.get("kind")
            if kind == "close":
                return
            if kind == "variables":
                variables = message.get("variables")
                if isinstance(variables, dict):
                    local.state.update(variables)
                continue
            if kind != "execute":
                continue
            try:
                result = local(str(message.get("code", "")))
                _executor_send(
                    conn,
                    {
                        "kind": "result",
                        "output": result.output,
                        "logs": result.logs,
                        "is_final_answer": result.is_final_answer,
                        "state": _executor_state_snapshot(local.state),
                    },
                )
            except BaseException as exc:
                with suppress(Exception):
                    _executor_send(
                        conn,
                        {
                            "kind": "error",
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "state": _executor_state_snapshot(local.state),
                        },
                    )
    finally:
        with suppress(Exception):
            conn.close()
        if liveness_conn is not None:
            with suppress(Exception):
                liveness_conn.close()


def _killable_executor_worker(
    conn: Any,
    executor: "KillablePythonExecutor",
    liveness_conn: Any | None = None,
    inherited_liveness_writer: Any | None = None,
    expected_parent_pid: int | None = None,
) -> None:
    """Run one persistent evaluator in a child process.

    The child receives the already-built capability functions through the
    POSIX fork boundary, but the parent only accepts bounded JSON results.  A
    timed-out call is terminated by the parent, so no evaluator thread can
    continue mutating state or invoking a capability after the timeout.
    """
    executor._conn = conn
    executor._in_worker = True
    try:
        process_group = _prepare_worker_lifecycle(
            liveness_conn, expected_parent_pid, inherited_liveness_writer
        )
        local = local_python_executor.LocalPythonExecutor(
            executor.additional_authorized_imports,
            max_print_outputs_length=executor.max_print_outputs_length,
            timeout_seconds=None,
        )
        local.state = executor.state
        local.static_tools = executor.static_tools
        local.custom_tools = executor.custom_tools
        with suppress(Exception):
            _executor_send(conn, {"kind": "ready", "process_group": process_group})
        while True:
            try:
                message = _executor_recv(conn)
            except (EOFError, OSError, ValueError, RuntimeError):
                return
            kind = message.get("kind")
            if kind == "close":
                return
            if kind == "variables":
                variables = message.get("variables")
                if isinstance(variables, dict):
                    local.state.update(variables)
                continue
            if kind != "execute":
                continue
            try:
                result = local(str(message.get("code", "")))
                _executor_send(
                    conn,
                    {
                        "kind": "result",
                        "output": result.output,
                        "logs": result.logs,
                        "is_final_answer": result.is_final_answer,
                        "state": _executor_state_snapshot(local.state),
                    },
                )
            except BaseException as exc:
                with suppress(Exception):
                    _executor_send(
                        conn,
                        {
                            "kind": "error",
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "state": _executor_state_snapshot(local.state),
                        },
                    )
    finally:
        with suppress(Exception):
            conn.close()


class _ProcessApprovalRequester:
    """Tool callback proxy that routes approval requests to the parent."""

    def __init__(self, executor: "KillablePythonExecutor"):
        self._executor = executor

    def __call__(self, request: dict[str, Any]) -> str:
        if self._executor._handling_child_tool_request:
            callback = self._executor._approval_callback
            if callback is None:
                return "reject"
            try:
                return str(callback(request) or "reject")
            except Exception:
                return "reject"
        return self._executor._request_approval_from_child(request)


class _ProcessOrchestrationRecorder:
    """Forward nested-agent records to the parent execution thread."""

    def __init__(self, executor: "KillablePythonExecutor"):
        self._executor = executor

    def __call__(self, record: dict[str, Any]) -> None:
        if self._executor._handling_child_tool_request:
            callback = self._executor._orchestration_callback
            if callback:
                with suppress(Exception):
                    callback(record)
            return
        self._executor._send_orchestration_record(record)


class KillablePythonExecutor(PythonExecutor):
    """A process-bound CodeAgent executor with a real wall-clock deadline.

    ``LocalPythonExecutor`` is intentionally retained inside the child for
    AST semantics and cross-step state, but it runs with its own timeout
    disabled.  The parent enforces the deadline and terminates the child, so
    a blocking call cannot outlive the reported timeout.  POSIX single-threaded
    callers use ``fork`` for compatibility with existing capability closures;
    multi-threaded callers and Windows use a spawn-safe RPC worker.  The
    executor never falls back to an in-process evaluator.
    """

    def __init__(
        self,
        additional_authorized_imports: list[str],
        max_print_outputs_length: int | None = None,
        timeout_seconds: float | None = DEFAULT_CODE_EXECUTION_TIMEOUT_SECONDS,
        *,
        force_spawn: bool = False,
    ):
        methods = multiprocessing.get_all_start_methods()
        if "fork" not in methods and "spawn" not in methods:
            raise RuntimeError("Python 执行器缺少 fork/spawn 进程边界")
        self.additional_authorized_imports = list(additional_authorized_imports)
        self.max_print_outputs_length = max_print_outputs_length
        self.timeout_seconds = timeout_seconds
        self.force_spawn = force_spawn
        self.state: dict[str, Any] = {"__name__": "__main__"}
        self.static_tools: dict[str, Any] | None = None
        self.custom_tools: dict[str, Any] = {}
        self.additional_functions: dict[str, Any] = {}
        self._fork_ctx = multiprocessing.get_context("fork") if "fork" in methods else None
        self._spawn_ctx = multiprocessing.get_context("spawn") if "spawn" in methods else None
        self._conn: Any | None = None
        self._process: Any | None = None
        self._process_group_id: int | None = None
        self._windows_job_handle: int | None = None
        self._liveness_conn: Any | None = None
        self._spawn_worker = False
        self._child_tool_names: list[str] = []
        self._child_function_names: list[str] = []
        self._closed = False
        self._approval_callback: Any | None = None
        self._orchestration_callback: Any | None = None
        self._in_worker = False
        self._handling_child_tool_request = False

    def approval_requester(self, callback: Any | None) -> Any | None:
        self._approval_callback = callback
        return _ProcessApprovalRequester(self) if callback else None

    def orchestration_recorder(self, callback: Any | None) -> Any | None:
        self._orchestration_callback = callback
        return _ProcessOrchestrationRecorder(self) if callback else None

    def send_variables(self, variables: dict[str, Any]) -> None:
        self.state.update(variables)
        if self._conn is not None and self._process is not None and self._process.is_alive():
            _executor_send(
                self._conn,
                {"kind": "variables", "variables": _executor_state_snapshot(variables)},
            )

    def send_tools(self, tools: dict[str, Any]) -> None:
        base_tools = getattr(local_python_executor, "BASE_PYTHON_TOOLS", {})
        self.static_tools = {
            **tools,
            **dict(base_tools),
            **self.additional_functions,
        }
        self._child_tool_names = list(tools)
        self._child_function_names = list(self.additional_functions)
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        if self._closed:
            raise RuntimeError("Python 执行器已关闭")
        if self._process is not None and self._process.is_alive():
            return
        if self._conn is not None:
            with suppress(Exception):
                self._conn.close()
        if self._liveness_conn is not None:
            with suppress(Exception):
                self._liveness_conn.close()
        # macOS can report a single active Python thread while Objective-C /
        # CoreFoundation worker threads and runtime locks still exist below
        # Python.  Avoid fork there; the spawn/RPC path is the portable
        # process boundary for Darwin and Windows.  External capabilities also
        # use spawn/RPC on Linux: fork would inherit and execute the callable
        # inside the child, preventing the parent from extending the deadline
        # across tool I/O and widening the child's inherited capability set.
        has_external_capabilities = bool(
            self._child_tool_names or self._child_function_names
        )
        use_fork = (
            not self.force_spawn
            and self._fork_ctx is not None
            and os.name == "posix"
            and not sys.platform.startswith("darwin")
            and threading.active_count() == 1
            and not has_external_capabilities
        )
        if not use_fork and self._spawn_ctx is None:
            raise RuntimeError("当前平台多线程执行缺少 spawn 进程边界")
        ctx = self._fork_ctx if use_fork else self._spawn_ctx
        self._spawn_worker = not use_fork
        self._process_group_id = None
        parent_conn, child_conn = ctx.Pipe(duplex=True)
        liveness_read, liveness_write = ctx.Pipe(duplex=False)
        self._conn = parent_conn
        self._liveness_conn = liveness_write
        if self._spawn_worker:
            target = _spawn_executor_worker
            args = (
                child_conn,
                self.additional_authorized_imports,
                self.max_print_outputs_length,
                self._child_tool_names,
                self._child_function_names,
                liveness_read,
                liveness_write,
                os.getpid(),
            )
        else:
            target = _killable_executor_worker
            args = (child_conn, self, liveness_read, liveness_write, os.getpid())
        process = ctx.Process(
            target=target,
            args=args,
            name="tim-code-executor",
        )
        process.daemon = False
        try:
            process.start()
        except BaseException:
            with suppress(Exception):
                child_conn.close()
            with suppress(Exception):
                liveness_read.close()
            with suppress(Exception):
                liveness_write.close()
            self._conn = None
            self._liveness_conn = None
            raise
        self._windows_job_handle = _windows_assign_kill_on_close_job(process.pid)
        with suppress(Exception):
            child_conn.close()
        with suppress(Exception):
            liveness_read.close()
        self._process = process
        if self._spawn_worker:
            _executor_send(
                self._conn,
                {
                    "kind": "variables",
                    "variables": _executor_state_snapshot(self.state),
                },
            )
        try:
            # Process startup and module import time are infrastructure
            # overhead, not user code execution.  Both fork and spawn workers
            # publish the same ready handshake; wait for it before starting
            # the first code deadline so short budgets remain meaningful on
            # every supported platform.
            if not self._conn.poll(5.0):
                raise RuntimeError("Python 执行器进程启动超时")
            ready = _executor_recv(self._conn)
            if ready.get("kind") != "ready":
                raise RuntimeError("Python 执行器握手失败")
            process_group_id = ready.get("process_group")
            if isinstance(process_group_id, int) and process_group_id > 1:
                self._process_group_id = process_group_id
        except BaseException:
            self._terminate_process()
            raise

    def _apply_state(self, snapshot: Any) -> None:
        if isinstance(snapshot, dict):
            self.state.update(snapshot)

    def _request_approval_from_child(self, request: dict[str, Any]) -> str:
        conn = self._conn
        if conn is None:
            return "reject"
        _executor_send(conn, {"kind": "approval_request", "request": request})
        while True:
            if not conn.poll(0.2):
                if not self._in_worker and (self._process is None or not self._process.is_alive()):
                    return "reject"
                continue
            try:
                message = _executor_recv(conn)
            except (EOFError, OSError, ValueError, RuntimeError):
                return "reject"
            if message.get("kind") == "approval_response":
                return str(message.get("decision") or "reject")
            if message.get("kind") == "stop":
                return "reject"

    def _send_orchestration_record(self, record: dict[str, Any]) -> None:
        conn = self._conn
        if conn is None:
            return
        with suppress(Exception):
            _executor_send(conn, {"kind": "orchestration", "record": record})

    def _dispatch_child_tool_request(self, message: dict[str, Any], conn: Any) -> None:
        name = str(message.get("name") or "")
        target = self.additional_functions.get(name)
        if target is None and self.static_tools is not None:
            target = self.static_tools.get(name)
        if not callable(target):
            _executor_send(conn, {"kind": "tool_response", "ok": False, "error": "工具不存在"})
            return
        args = message.get("args")
        kwargs = message.get("kwargs")
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            _executor_send(conn, {"kind": "tool_response", "ok": False, "error": "工具参数无效"})
            return
        self._handling_child_tool_request = True
        try:
            result = target(*args, **kwargs)
            _executor_send(conn, {"kind": "tool_response", "ok": True, "result": result})
        except BaseException as exc:
            with suppress(Exception):
                _executor_send(
                    conn,
                    {
                        "kind": "tool_response",
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
        finally:
            self._handling_child_tool_request = False

    def _terminate_process(self) -> None:
        process = self._process
        self._process = None
        process_group_id = self._process_group_id
        self._process_group_id = None
        windows_job_handle = self._windows_job_handle
        self._windows_job_handle = None
        conn = self._conn
        self._conn = None
        liveness_conn = self._liveness_conn
        self._liveness_conn = None
        if liveness_conn is not None:
            with suppress(Exception):
                liveness_conn.close()
        _windows_close_job(windows_job_handle)
        if conn is not None:
            with suppress(Exception):
                conn.close()
        if process is None:
            return
        if process.is_alive():
            if process_group_id and os.name == "posix":
                with suppress(OSError):
                    os.killpg(process_group_id, signal.SIGTERM)
            elif os.name == "nt":
                # ``multiprocessing`` has no portable process-group API on
                # Windows.  taskkill /T asks the kernel to terminate the
                # worker tree before the direct handle fallback below.
                with suppress(Exception):
                    subprocess.run(
                        ["taskkill", "/PID", str(int(process.pid)), "/T", "/F"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
            process.terminate()
            process.join(timeout=0.5)
            if process.is_alive() and hasattr(process, "kill"):
                if process_group_id and os.name == "posix":
                    with suppress(OSError):
                        os.killpg(process_group_id, signal.SIGKILL)
                process.kill()
                process.join(timeout=0.5)
        else:
            process.join(timeout=0.1)

    def terminate(self) -> None:
        """Interrupt the current code call, used by AgentRun.stop_run."""
        self._terminate_process()

    def __call__(self, code_action: str) -> CodeOutput:
        self._ensure_worker()
        conn = self._conn
        if conn is None:
            raise RuntimeError("Python 执行器 IPC 未初始化")
        _executor_send(conn, {"kind": "execute", "code": code_action})
        deadline = None if self.timeout_seconds is None else time.monotonic() + float(self.timeout_seconds)
        while True:
            if deadline is None:
                wait_seconds = 0.2
            else:
                wait_seconds = min(0.2, deadline - time.monotonic())
                if wait_seconds <= 0:
                    self._terminate_process()
                    raise local_python_executor.ExecutionTimeoutError(
                        f"Code execution exceeded the maximum execution time of {self.timeout_seconds} seconds"
                    )
            if not conn.poll(wait_seconds):
                if self._process is None or not self._process.is_alive():
                    self._terminate_process()
                    raise RuntimeError("Python 执行器进程意外退出")
                continue
            try:
                message = _executor_recv(conn)
            except (EOFError, OSError, ValueError, RuntimeError) as exc:
                self._terminate_process()
                raise RuntimeError("Python 执行器进程意外退出") from exc
            kind = message.get("kind")
            if kind == "ready":
                process_group_id = message.get("process_group")
                if isinstance(process_group_id, int) and process_group_id > 1:
                    self._process_group_id = process_group_id
                continue
            if kind == "approval_request":
                callback = self._approval_callback
                decision = callback(message.get("request") or {}) if callback else "reject"
                _executor_send(conn, {"kind": "approval_response", "decision": decision})
                # Approval waiting is user time, matching RunSkillTool's
                # deadline semantics.  Start a fresh code budget after the
                # decision so a slow human response is not misclassified as
                # a Python execution timeout.
                if self.timeout_seconds is not None:
                    deadline = time.monotonic() + float(self.timeout_seconds)
                continue
            if kind == "tool_request":
                # Capability execution happens in the parent and may block on
                # network I/O or operator approval.  That wait is outside the
                # Python code budget; extend the deadline by the elapsed
                # dispatch time so a successful tool return is not followed by
                # an artificial ExecutionTimeoutError.
                tool_started = time.monotonic()
                self._dispatch_child_tool_request(message, conn)
                if deadline is not None:
                    deadline += max(0.0, time.monotonic() - tool_started)
                continue
            if kind == "orchestration":
                callback = self._orchestration_callback
                if callback:
                    with suppress(Exception):
                        callback(message.get("record") or {})
                continue
            if kind == "result":
                self._apply_state(message.get("state"))
                return CodeOutput(
                    output=message.get("output"),
                    logs=str(message.get("logs") or ""),
                    is_final_answer=bool(message.get("is_final_answer")),
                )
            if kind == "error":
                self._apply_state(message.get("state"))
                error_type = str(message.get("error_type") or "RuntimeError")
                error_class = getattr(local_python_executor, error_type, RuntimeError)
                if not isinstance(error_class, type) or not issubclass(error_class, Exception):
                    error_class = RuntimeError
                raise error_class(str(message.get("error") or "Python 执行失败"))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._terminate_process()


def _nsjail_send(sock: socket.socket, message: dict[str, Any]) -> None:
    payload = json.dumps(
        _executor_wire_value(message, budget=[MAX_EXECUTOR_IPC_BYTES]),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(payload) > MAX_EXECUTOR_IPC_BYTES:
        raise RuntimeError("nsjail worker IPC 消息超过安全上限")
    sock.sendall(struct.pack("!I", len(payload)) + payload)


def _nsjail_read_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise EOFError("nsjail worker 已断开 RPC")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _nsjail_recv(sock: socket.socket, timeout: float | None = None) -> dict[str, Any] | None:
    if timeout is not None:
        readable, _, _ = select.select([sock], [], [], max(0.0, timeout))
        if not readable:
            return None
    header = _nsjail_read_exact(sock, 4)
    size = struct.unpack("!I", header)[0]
    if size > MAX_EXECUTOR_IPC_BYTES:
        raise RuntimeError("nsjail worker IPC 消息超过安全上限")
    message = json.loads(_nsjail_read_exact(sock, size).decode("utf-8"))
    if not isinstance(message, dict):
        raise RuntimeError("nsjail worker IPC 消息格式无效")
    return message


class NsJailPythonExecutor(PythonExecutor):
    """Run the AST evaluator in a separate nsjail process with RPC tools.

    The worker never receives application ``Tool`` instances.  It receives
    only proxy names; the parent dispatches each capability call, so cloud
    clients, BotManager, and AgentEngine stay outside the jailed process.
    This mode is intentionally opt-in because the nsjail binary and a
    deployment-provided rootfs are Linux host dependencies.
    """

    def __init__(
        self,
        additional_authorized_imports: list[str],
        max_print_outputs_length: int | None = None,
        timeout_seconds: float | None = DEFAULT_CODE_EXECUTION_TIMEOUT_SECONDS,
    ):
        if os.name != "posix" or not sys.platform.startswith("linux"):
            raise RuntimeError("nsjail 执行器只支持 Linux 部署")
        self.nsjail_path = os.environ.get("TIM_NSJAIL_BIN") or shutil.which("nsjail")
        if not self.nsjail_path:
            raise RuntimeError("未找到 nsjail；生产环境拒绝回退到进程内执行器")
        self.rootfs = os.environ.get("TIM_NSJAIL_ROOT")
        if not self.rootfs or not os.path.isdir(self.rootfs):
            raise RuntimeError("TIM_NSJAIL_ROOT 未配置或目录不存在；拒绝启动 nsjail worker")
        self.seccomp_policy = os.environ.get("TIM_NSJAIL_SECCOMP_POLICY")
        if not self.seccomp_policy or not os.path.isfile(self.seccomp_policy):
            raise RuntimeError("TIM_NSJAIL_SECCOMP_POLICY 未配置；拒绝启动无 seccomp 的 worker")
        self.additional_authorized_imports = list(additional_authorized_imports)
        self.max_print_outputs_length = max_print_outputs_length
        self.timeout_seconds = timeout_seconds
        self.state: dict[str, Any] = {"__name__": "__main__"}
        self.static_tools: dict[str, Any] | None = None
        self.custom_tools: dict[str, Any] = {}
        self.additional_functions: dict[str, Any] = {}
        self._tools: dict[str, Any] = {}
        self._process: subprocess.Popen[bytes] | None = None
        self._socket: socket.socket | None = None
        self._closed = False
        self._approval_callback: Any | None = None
        self._orchestration_callback: Any | None = None

    def approval_requester(self, callback: Any | None) -> Any | None:
        self._approval_callback = callback
        return callback

    def orchestration_recorder(self, callback: Any | None) -> Any | None:
        self._orchestration_callback = callback
        return callback

    def send_variables(self, variables: dict[str, Any]) -> None:
        self.state.update(variables)
        if self._socket is not None and self._process is not None and self._process.poll() is None:
            _nsjail_send(self._socket, {"kind": "variables", "variables": _executor_state_snapshot(variables)})

    def send_tools(self, tools: dict[str, Any]) -> None:
        self._tools = dict(tools)
        self.static_tools = {**tools, **self.additional_functions}
        self._ensure_worker()
        assert self._socket is not None
        _nsjail_send(
            self._socket,
            {
                "kind": "init",
                "tool_names": list(self._tools),
                "function_names": list(self.additional_functions),
                "state": _executor_state_snapshot(self.state),
            },
        )
        ready = _nsjail_recv(self._socket, timeout=5.0)
        if not ready or ready.get("kind") != "initialized":
            self._terminate_process()
            raise RuntimeError("nsjail worker 初始化失败")

    def _ensure_worker(self) -> None:
        if self._closed:
            raise RuntimeError("nsjail Python 执行器已关闭")
        if self._process is not None and self._process.poll() is None:
            return
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        child, parent = socket.socketpair()
        child_fd = child.fileno()
        child.set_inheritable(True)
        python_path = os.environ.get("TIM_NSJAIL_PYTHON", "/usr/bin/python3")
        time_limit = max(1, int(math.ceil(float(self.timeout_seconds or 30.0) + 5)))
        command = [
            self.nsjail_path,
            "--mode",
            "o",
            "--quiet",
            "--pass_fd",
            str(child_fd),
            "--iface_no_lo",
            "--disable_proc",
            "--time_limit",
            str(time_limit),
            "--rlimit_cpu",
            str(max(1, int(math.ceil(float(self.timeout_seconds or 30.0))))),
            "--rlimit_as",
            os.environ.get("TIM_NSJAIL_RLIMIT_AS_MB", "1024"),
            "--rlimit_nofile",
            os.environ.get("TIM_NSJAIL_RLIMIT_NOFILE", "256"),
            "--rlimit_nproc",
            os.environ.get("TIM_NSJAIL_RLIMIT_NPROC", "32"),
            "--tmpfsmount",
            "/tmp",
            "--seccomp_policy",
            self.seccomp_policy,
            "--chroot",
            self.rootfs,
            "--bindmount_ro",
            f"{repo_root}:/app",
            "--cwd",
            "/app",
            "--env",
            "PYTHONPATH=/app",
            "--env",
            "PYTHONUNBUFFERED=1",
            "--",
            python_path,
            "-m",
            "core.agent.nsjail_worker",
            "--fd",
            str(child_fd),
            "--imports-json",
            json.dumps(self.additional_authorized_imports, ensure_ascii=False),
        ]
        try:
            process = subprocess.Popen(
                command,
                cwd=repo_root,
                env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": repo_root, "PYTHONUNBUFFERED": "1"},
                pass_fds=(child_fd,),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            child.close()
            parent.close()
            raise
        child.close()
        self._socket = parent
        self._process = process
        try:
            ready = _nsjail_recv(parent, timeout=5.0)
            if not ready or ready.get("kind") != "ready":
                raise RuntimeError("nsjail worker 未就绪")
        except Exception:
            self._terminate_process()
            raise

    def _dispatch_capability(self, message: dict[str, Any]) -> Any:
        name = str(message.get("name") or "")
        args = message.get("args") or []
        kwargs = message.get("kwargs") or {}
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            raise ValueError("RPC capability 参数格式无效")
        kind = message.get("kind")
        if kind == "function_call" and name in self.additional_functions:
            return self.additional_functions[name](*args, **kwargs)
        if kind == "tool_call" and name in self._tools:
            tool = self._tools[name]
            return tool(*args, **kwargs)
        raise RuntimeError(f"nsjail worker 请求了未注册能力或类型不匹配: {name}")

    def _terminate_process(self) -> None:
        process = self._process
        self._process = None
        sock = self._socket
        self._socket = None
        if sock is not None:
            with suppress(Exception):
                sock.close()
        if process is None:
            return
        if process.poll() is None:
            with suppress(OSError):
                os.killpg(process.pid, signal.SIGTERM)
            with suppress(Exception):
                process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                with suppress(OSError):
                    os.killpg(process.pid, signal.SIGKILL)
                with suppress(Exception):
                    process.kill()
                with suppress(Exception):
                    process.wait(timeout=0.5)
        else:
            with suppress(Exception):
                process.wait(timeout=0.1)

    def terminate(self) -> None:
        self._terminate_process()

    def __call__(self, code_action: str) -> CodeOutput:
        self._ensure_worker()
        if self._socket is None:
            raise RuntimeError("nsjail worker RPC 未初始化")
        _nsjail_send(self._socket, {"kind": "execute", "code": code_action})
        deadline = None if self.timeout_seconds is None else time.monotonic() + float(self.timeout_seconds)
        while True:
            if deadline is None:
                wait = 0.2
            else:
                wait = min(0.2, deadline - time.monotonic())
                if wait <= 0:
                    self._terminate_process()
                    raise local_python_executor.ExecutionTimeoutError(
                        f"Code execution exceeded the maximum execution time of {self.timeout_seconds} seconds"
                    )
            message = _nsjail_recv(self._socket, timeout=wait)
            if message is None:
                if self._process is None or self._process.poll() is not None:
                    self._terminate_process()
                    raise RuntimeError("nsjail worker 进程意外退出")
                continue
            kind = message.get("kind")
            if kind in {"tool_call", "function_call"}:
                # Parent-side capabilities can wait on network services or a
                # human approval decision.  Do not charge that wait against
                # the child Python evaluation budget.
                tool_started = time.monotonic()
                try:
                    value = self._dispatch_capability(message)
                    _nsjail_send(self._socket, {"kind": "response", "ok": True, "value": value})
                except Exception as exc:
                    _nsjail_send(self._socket, {"kind": "response", "ok": False, "error": str(exc)})
                finally:
                    if deadline is not None:
                        deadline += max(0.0, time.monotonic() - tool_started)
                continue
            if kind == "result":
                self._apply_state(message.get("state"))
                return CodeOutput(
                    output=message.get("output"),
                    logs=str(message.get("logs") or ""),
                    is_final_answer=bool(message.get("is_final_answer")),
                )
            if kind == "error":
                self._apply_state(message.get("state"))
                error_type = str(message.get("error_type") or "RuntimeError")
                error_class = getattr(local_python_executor, error_type, RuntimeError)
                if not isinstance(error_class, type) or not issubclass(error_class, Exception):
                    error_class = RuntimeError
                raise error_class(str(message.get("error") or "Python 执行失败"))

    def _apply_state(self, snapshot: Any) -> None:
        if isinstance(snapshot, dict):
            self.state.update(snapshot)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._socket is not None and self._process is not None and self._process.poll() is None:
            with suppress(Exception):
                _nsjail_send(self._socket, {"kind": "close"})
        self._terminate_process()


def _create_code_executor(
    additional_authorized_imports: list[str],
    *,
    max_print_outputs_length: int | None = None,
    timeout_seconds: float | None = DEFAULT_CODE_EXECUTION_TIMEOUT_SECONDS,
) -> PythonExecutor:
    """Select the execution boundary without silently weakening production.

    Linux production/staging must use nsjail.  macOS and Windows have no
    nsjail deployment contract, so their explicitly controlled fallback uses
    the spawn/RPC executor and is reported as lacking OS-level isolation.
    """
    mode = os.environ.get("TIM_CODE_EXECUTOR_MODE", "fork").strip().lower()
    app_env = os.environ.get("APP_ENV", "development").strip().lower()
    production = app_env in {"production", "prod", "staging"}
    linux_host = os.name == "posix" and sys.platform.startswith("linux")
    if mode not in {"fork", "spawn", "nsjail"}:
        raise RuntimeError("TIM_CODE_EXECUTOR_MODE 必须为 fork 或 nsjail；非 Linux 可额外使用 spawn")
    allow_unconfined = os.environ.get("ALLOW_UNCONFINED_CODE_EXECUTOR", "").strip().lower() in {"1", "true", "yes", "on"}
    if production and linux_host and mode != "nsjail" and not allow_unconfined:
        raise RuntimeError("Linux 生产环境必须设置 TIM_CODE_EXECUTOR_MODE=nsjail，或显式设置 ALLOW_UNCONFINED_CODE_EXECUTOR=true")
    if production and mode in {"fork", "spawn"} and (not linux_host or allow_unconfined):
        logging.getLogger(__name__).warning(
            "生产环境使用受控 spawn/RPC executor；该路径不提供 nsjail 的 OS 级隔离，"
            "仅适用于已评估风险的受信部署，不得将其宣称为 seccomp、namespace 或 rootfs 沙箱"
        )
    executor_type = NsJailPythonExecutor if mode == "nsjail" else KillablePythonExecutor
    executor_kwargs: dict[str, Any] = {
        "max_print_outputs_length": max_print_outputs_length,
        "timeout_seconds": timeout_seconds,
    }
    if mode in {"fork", "spawn"}:
        executor_kwargs["force_spawn"] = mode == "spawn" or (production and (not linux_host or allow_unconfined))
    return executor_type(
        additional_authorized_imports,
        **executor_kwargs,
    )


def _install_local_executor_safety_limits() -> None:
    """Patch smolagents' cooperative evaluator without claiming hard isolation.

    smolagents' stock timeout uses a context manager whose shutdown waits for
    the worker thread after a timeout.  Returning immediately prevents the
    request thread from being held by a finite blocking call; the worker is
    still not killable, so production hard isolation remains a process/host
    boundary requirement.
    """
    if not getattr(local_python_executor, "_tim_timeout_patched", False):
        def nonblocking_timeout(timeout_seconds: int):
            def decorator(func):
                @wraps(func)
                def wrapper(*args, **kwargs):
                    executor = ThreadPoolExecutor(max_workers=1)
                    future: Future = executor.submit(func, *args, **kwargs)
                    try:
                        return future.result(timeout=timeout_seconds)
                    except FuturesTimeoutError as exc:
                        future.cancel()
                        executor.shutdown(wait=False, cancel_futures=True)
                        raise local_python_executor.ExecutionTimeoutError(
                            f"Code execution exceeded the maximum execution time of {timeout_seconds} seconds"
                        ) from exc
                    except BaseException:
                        executor.shutdown(wait=True, cancel_futures=True)
                        raise
                    else:
                        executor.shutdown(wait=True, cancel_futures=True)

                return wrapper

            return decorator

        local_python_executor.timeout = nonblocking_timeout
        local_python_executor._tim_timeout_patched = True

    if not getattr(local_python_executor, "_tim_for_loop_patched", False):
        def bounded_evaluate_for(for_loop, state, static_tools, custom_tools, authorized_imports):
            result = None
            iterator = local_python_executor.evaluate_ast(
                for_loop.iter, state, static_tools, custom_tools, authorized_imports
            )
            for iteration, counter in enumerate(iterator, start=1):
                if iteration > MAX_SANDBOX_FOR_ITERATIONS:
                    raise local_python_executor.InterpreterError(
                        f"Maximum number of {MAX_SANDBOX_FOR_ITERATIONS} for-loop iterations exceeded"
                    )
                local_python_executor.set_value(
                    for_loop.target,
                    counter,
                    state,
                    static_tools,
                    custom_tools,
                    authorized_imports,
                )
                for node in for_loop.body:
                    try:
                        line_result = local_python_executor.evaluate_ast(
                            node, state, static_tools, custom_tools, authorized_imports
                        )
                        if line_result is not None:
                            result = line_result
                    except local_python_executor.BreakException:
                        return result
                    except local_python_executor.ContinueException:
                        break
            return result

        local_python_executor.evaluate_for = bounded_evaluate_for
        local_python_executor._tim_for_loop_patched = True

    # Keep the dependency's built-in while-loop guard conservative as an
    # additional evaluator limit.  This is version-sensitive defense in depth,
    # not a substitute for a killable worker process.
    if hasattr(local_python_executor, "MAX_WHILE_ITERATIONS"):
        local_python_executor.MAX_WHILE_ITERATIONS = min(
            local_python_executor.MAX_WHILE_ITERATIONS,
            MAX_SANDBOX_FOR_ITERATIONS,
        )


_install_local_executor_safety_limits()


def _asset_temp_path(destination: str) -> str:
    stem, suffix = os.path.splitext(destination)
    return f"{stem}.{os.getpid()}.{threading.get_ident()}.tmp{suffix}"


def _commit_generated_asset(temp_path: str, destination: str, uploads_dir: str) -> int:
    """Apply generated-file quotas before exposing the asset to downloads."""
    try:
        file_size = os.path.getsize(temp_path)
        if file_size > MAX_GENERATED_FILE_BYTES:
            raise ValueError("生成文件超过 100 MiB 限制")
        existing_files = []
        for entry in os.scandir(uploads_dir):
            if entry.name in {os.path.basename(temp_path), os.path.basename(destination)}:
                continue
            if entry.is_file(follow_symlinks=False):
                existing_files.append(entry)
        if len(existing_files) + (0 if os.path.exists(destination) else 1) > MAX_GENERATED_FILES:
            raise ValueError("智能体生成文件数量已达到上限")
        existing_bytes = sum(entry.stat(follow_symlinks=False).st_size for entry in existing_files)
        if existing_bytes + file_size > MAX_GENERATED_TOTAL_BYTES:
            raise ValueError("智能体生成文件总大小已达到 512 MiB 限制")
        os.replace(temp_path, destination)
        with _SANDBOX_FILE_QUOTA_LOCK:
            _SANDBOX_QUOTA_TOTALS.pop(uploads_dir, None)
        return file_size
    except Exception:
        with suppress(OSError):
            os.remove(temp_path)
        raise

# 增强 smolagents 代码块解析器：无缝兼容 DeepSeek 原生 DSML、标准 Markdown 与 XML parameter
_original_parse_code_blobs = getattr(saa, "parse_code_blobs", None)


def robust_parse_code_blobs(text: str, code_block_tags: tuple[str, str]) -> str:
    # 1. 尝试原厂解析
    if _original_parse_code_blobs:
        try:
            return _original_parse_code_blobs(text, code_block_tags)
        except Exception as parse_error:
            logger.debug("Primary code parser failed: %s", parse_error)

    # 2. 兼容 DeepSeek 原生 DSML 结构: <parameter name="code">...</parameter>
    dsml_match = re.search(
        r"<parameter name=[\"\']code[\"\']>([\s\S]*?)</parameter>", text
    )
    if dsml_match:
        code = dsml_match.group(1).strip()
        if code:
            return code

    # 3. 兼容通用 XML/HTML 标签 <code>...</code>, <python>...</python>, <execute>...</execute>
    tag_match = re.search(
        r"<(?:code|python|execute)>([\s\S]*?)</(?:code|python|execute)>",
        text,
        re.IGNORECASE,
    )
    if tag_match:
        code = tag_match.group(1).strip()
        if code:
            return code

    # 4. 回退
    if _original_parse_code_blobs:
        return _original_parse_code_blobs(text, code_block_tags)
    return text


if _original_parse_code_blobs:
    saa.parse_code_blobs = robust_parse_code_blobs

logger = logging.getLogger("tim.agent.engine")

# Keep persistent Python objects bounded.  A sandbox state may contain large
# dataframes or other user-created objects, so an unbounded per-user map would
# retain memory until the bot is explicitly deleted.
DEFAULT_SANDBOX_MAX_ENTRIES = 50


def _install_sandbox_scope(agent: Any, scope: dict[str, Any]) -> None:
    """Install sandbox callables/constants through smolagents' supported channels.

    ``CodeAgent.run`` calls ``send_tools`` before executing code and that method
    rebuilds ``static_tools``.  Direct writes to ``python_executor.static_tools``
    are therefore ephemeral.  Callables belong in ``additional_functions``;
    values such as output directories belong in the agent state sent as
    variables.
    """
    executor = getattr(agent, "python_executor", None)
    if executor is None:
        raise RuntimeError("CodeAgent 未初始化 Python executor")

    additional_functions = getattr(executor, "additional_functions", None)
    if not isinstance(additional_functions, dict):
        additional_functions = {}
        executor.additional_functions = additional_functions

    forbidden_names = {"cloud_client", "tim_client", "get_usersig"}
    function_scope = {
        name: value
        for name, value in scope.items()
        if name not in forbidden_names and inspect.isroutine(value)
    }
    state_scope = {
        name: value
        for name, value in scope.items()
        if name not in function_scope and name not in forbidden_names
    }
    for name in forbidden_names:
        agent.state.pop(name, None)
        additional_functions.pop(name, None)
    additional_functions.update(function_scope)
    agent.state.update(state_scope)


def render_chart(
    chart_type: str,
    title: str,
    labels: list[Any],
    data: list[Any],
    series_name: str | None = None,
    options: dict[str, Any] | None = None,
) -> str:
    """
    生成就地渲染的 ECharts 交互式图表 Markdown 代码块。
    支持在 Python 沙箱中调用并打印，前端 MarkdownView 自动将其解析为可视化图表。
    """
    chart_cfg: dict[str, Any] = {
        "type": str(chart_type).lower(),
        "title": str(title),
        "labels": list(labels),
        "data": list(data),
    }
    if series_name:
        chart_cfg["seriesName"] = str(series_name)
    if options and isinstance(options, dict):
        chart_cfg["options"] = options
    return f"\n```echarts\n{json.dumps(chart_cfg, ensure_ascii=False, indent=2)}\n```\n"


def _sandbox_regular_file_bytes(root_dir: str) -> int:
    """Return the current size of regular files directly in ``root_dir``.

    Model code can only address basenames, so a flat directory scan is enough
    here and avoids following links that might point outside the bot runtime.
    A missing directory is treated as empty; the subsequent ``open`` call will
    still report the ordinary filesystem error when appropriate.
    """
    total = 0
    try:
        entries = os.scandir(root_dir)
    except OSError:
        return 0
    with entries:
        for entry in entries:
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                # Files can disappear while the directory is being scanned.
                # The next quota check will observe the new state.
                continue
    return total


def _sandbox_write_size(file_obj: Any, data: Any) -> int:
    """Estimate the bytes a file object's ``write`` call will append.

    Text streams expose their encoding and error policy.  Computing the encoded
    size before writing lets the proxy reject an over-quota operation without
    partially modifying the file.  Newline translation is platform-native and
    is a no-op on the Linux workers where the sandbox runs.
    """
    if isinstance(data, str):
        encoding = getattr(file_obj, "encoding", None) or "utf-8"
        errors = getattr(file_obj, "errors", None) or "strict"
        return len(data.encode(encoding, errors))
    if isinstance(data, (bytes, bytearray, memoryview)):
        return memoryview(data).nbytes
    # Let the underlying stream produce its normal TypeError for unsupported
    # values.  Returning zero here would make the quota check misleading.
    raise TypeError("a bytes-like object or string is required")


class _QuotaFile:
    """A file-object proxy that enforces bot output quotas efficiently.

    Quota accounting is incremental after the handle is opened.  Opening a
    handle performs one directory scan to establish the aggregate baseline;
    repeated small writes update the shared total by their size delta instead
    of flushing and rescanning the whole directory on every call.
    """

    __slots__ = ("__weakref__",)

    def __init__(self, file_obj: Any, root_dir: str, candidate: str, mode: str):
        with _SANDBOX_FILE_QUOTA_LOCK:
            # Opening in ``w`` may have truncated an existing file, so scan
            # after open.  This is the only normal full-directory scan for a
            # handle's lifetime; later writes use the shared delta counter.
            current_size = self._native_size(file_obj, candidate)
            _SANDBOX_QUOTA_TOTALS[root_dir] = _sandbox_regular_file_bytes(root_dir)
            _SANDBOX_QUOTA_FILES[self] = {
                "file": file_obj,
                "root": root_dir,
                "path": candidate,
                "mode": mode,
                "size": current_size,
                "closed": False,
            }

    @staticmethod
    def _native_size(file_obj: Any, candidate: str) -> int:
        try:
            return int(os.fstat(file_obj.fileno()).st_size)
        except (AttributeError, OSError, ValueError):
            try:
                return int(os.path.getsize(candidate))
            except OSError:
                return 0

    def __getattribute__(self, name: str) -> Any:
        if name == "_state":
            raise AttributeError("沙箱文件对象不提供 _state 属性")
        return object.__getattribute__(self, name)

    def __getattr__(self, name: str) -> Any:
        if name == "_state" or name in _SANDBOX_FILE_ESCAPE_ATTRIBUTES:
            raise AttributeError(f"沙箱文件对象不提供 {name} 属性")
        return getattr(_sandbox_quota_file_state(self)["file"], name)

    def __enter__(self):
        _sandbox_quota_file_state(self)["file"].__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def close(self):
        state = _sandbox_quota_file_state(self)
        if state["closed"]:
            return None
        with _SANDBOX_FILE_QUOTA_LOCK:
            file_obj = state["file"]
            try:
                file_obj.flush()
                actual_size = self._native_size(file_obj, state["path"])
                _SANDBOX_QUOTA_TOTALS[state["root"]] = (
                    _SANDBOX_QUOTA_TOTALS.get(state["root"], 0)
                    + actual_size - state["size"]
                )
                state["size"] = actual_size
            finally:
                state["closed"] = True
            return file_obj.close()

    def __iter__(self):
        return self

    def __next__(self):
        return next(_sandbox_quota_file_state(self)["file"])

    def _stream_size(self) -> int:
        return int(_sandbox_quota_file_state(self)["size"])

    def _stream_position(self, current_size: int) -> int:
        state = _sandbox_quota_file_state(self)
        if "a" in state["mode"]:
            return current_size
        try:
            position = state["file"].tell()
            return max(0, int(position))
        except (AttributeError, OSError, ValueError, TypeError):
            return current_size

    def _check_projected_size(self, projected_size: int, current_size: int) -> None:
        if projected_size > MAX_GENERATED_FILE_BYTES:
            raise ValueError("生成文件超过 100 MiB 限制")
        state = _sandbox_quota_file_state(self)
        total = _SANDBOX_QUOTA_TOTALS.get(state["root"])
        if total is None:
            total = _sandbox_regular_file_bytes(state["root"])
            _SANDBOX_QUOTA_TOTALS[state["root"]] = total
        if total - current_size + projected_size > MAX_GENERATED_TOTAL_BYTES:
            raise ValueError("智能体生成文件总大小已达到 512 MiB 限制")

    def write(self, data: Any):
        with _SANDBOX_FILE_QUOTA_LOCK:
            state = _sandbox_quota_file_state(self)
            current_size = int(state["size"])
            position = self._stream_position(current_size)
            payload_size = _sandbox_write_size(state["file"], data)
            projected_size = max(current_size, position + payload_size)
            self._check_projected_size(projected_size, current_size)
            result = state["file"].write(data)
            # Account for the logical stream size.  The native buffer is
            # flushed once on close, avoiding a syscall per tiny write.
            delta = projected_size - current_size
            state["size"] = projected_size
            _SANDBOX_QUOTA_TOTALS[state["root"]] = (
                _SANDBOX_QUOTA_TOTALS.get(state["root"], 0) + delta
            )
            return result

    def writelines(self, lines: Iterable[Any]):
        for line in lines:
            self.write(line)

    def truncate(self, size: int | None = None):
        with _SANDBOX_FILE_QUOTA_LOCK:
            state = _sandbox_quota_file_state(self)
            current_size = int(state["size"])
            if size is None:
                size = self._stream_position(current_size)
            try:
                target_size = int(size)
            except (TypeError, ValueError) as exc:
                raise TypeError("truncate size must be an integer") from exc
            if target_size < 0:
                raise ValueError("negative truncate size")
            self._check_projected_size(target_size, current_size)
            result = state["file"].truncate(target_size)
            delta = target_size - current_size
            state["size"] = target_size
            _SANDBOX_QUOTA_TOTALS[state["root"]] = (
                _SANDBOX_QUOTA_TOTALS.get(state["root"], 0) + delta
            )
            return result


def _confined_open(root_dir: str, file, mode="r", *args, **kwargs):
    """Open a basename inside a bot directory with bounded write capability."""
    raw_path = os.fspath(file)
    if isinstance(raw_path, bytes):
        raw_path = os.fsdecode(raw_path)
    if not isinstance(raw_path, str):
        raise TypeError("沙箱文件路径必须是字符串或 PathLike")
    root_path = os.path.realpath(root_dir)
    candidate = os.path.realpath(os.path.join(root_path, os.path.basename(raw_path)))
    if os.path.commonpath([root_path, candidate]) != root_path:
        raise PermissionError("沙箱文件路径超出 BOT 产出物目录")
    try:
        file_obj = open(candidate, mode, *args, **kwargs)
        if any(flag in mode for flag in "wax+"):
            return _QuotaFile(file_obj, root_path, candidate, mode)
        return file_obj
    except OSError as exc:
        raise OSError(f"无法访问沙箱文件: {candidate}") from exc


class RobustStreamingOpenAIServerModel(OpenAIServerModel):
    """支持 DeepSeek-R1 思考链流式输出、优雅降级与真正 Token 级 SSE 推送的大模型包装器"""

    def generate_stream(
        self,
        messages,
        stop_sequences=None,
        response_format=None,
        tools_to_call_from=None,
        **kwargs,
    ):
        completion_kwargs = self._prepare_completion_kwargs(
            messages=messages,
            stop_sequences=stop_sequences,
            response_format=response_format,
            tools_to_call_from=tools_to_call_from,
            model=self.model_id,
            custom_role_conversions=self.custom_role_conversions,
            convert_images_to_image_urls=True,
            **kwargs,
        )
        self._apply_rate_limit()

        try:
            stream_response = self.retryer(
                self.client.chat.completions.create,
                **completion_kwargs,
                stream=True,
                stream_options={"include_usage": True},
            )
        except Exception:
            # 兼容不支持 stream_options 的提供商 (如 DashScope / 本地 Ollama / 某些转发中转)
            stream_response = self.retryer(
                self.client.chat.completions.create,
                **completion_kwargs,
                stream=True,
            )

        in_reasoning = False
        for event in stream_response:
            if getattr(event, "usage", None):
                yield ChatMessageStreamDelta(
                    content="",
                    token_usage=TokenUsage(
                        input_tokens=event.usage.prompt_tokens,
                        output_tokens=event.usage.completion_tokens,
                    ),
                )
            if getattr(event, "choices", None) and len(event.choices) > 0:
                choice = event.choices[0]
                if choice.delta:
                    # 识别 DeepSeek Reasoner / QwQ 等模型的原生 reasoning_content
                    reasoning = getattr(choice.delta, "reasoning_content", None)
                    content = choice.delta.content

                    if reasoning:
                        if not in_reasoning:
                            yield ChatMessageStreamDelta(content="<think>")
                            in_reasoning = True
                        yield ChatMessageStreamDelta(content=reasoning)

                    if content:
                        if in_reasoning:
                            yield ChatMessageStreamDelta(content="</think>")
                            in_reasoning = False
                        yield ChatMessageStreamDelta(
                            content=content,
                            tool_calls=[
                                ChatMessageToolCallStreamDelta(
                                    index=delta.index,
                                    id=delta.id,
                                    type=delta.type,
                                    function=delta.function,
                                )
                                for delta in choice.delta.tool_calls
                            ]
                            if choice.delta.tool_calls
                            else None,
                        )
        if in_reasoning:
            yield ChatMessageStreamDelta(content="</think>")


def extract_clean_thought(raw_text: str) -> str:
    """从模型原始输出中抽取纯净的思维链内容，剔除混入的代码块与多余格式标签"""
    if not raw_text:
        return ""
    text = str(raw_text).strip()
    # 1. 抽取 <think> 标签内容 (DeepSeek-R1 / Qwen 等推理模型)
    think_match = re.search(r"<think>([\s\S]*?)(?:</think>|$)", text, re.IGNORECASE)
    if think_match:
        return think_match.group(1).strip()

    # 2. 截取代码块之前的所有思考文本
    lines = text.split("\n")
    thought_lines = []
    for line in lines:
        if line.strip().startswith("```"):
            break
        thought_lines.append(line)
    thought = "\n".join(thought_lines).strip()

    # 3. 移除前导思考标记
    for prefix in ["thought:", "思考:", "思考：", "思维链:", "思维链："]:
        if thought.lower().startswith(prefix):
            thought = thought[len(prefix) :].strip()
            break
    return thought


def clean_final_answer(raw_answer: str) -> tuple[str, str]:
    """清洗最终回复，剥离残留的 <think> 思维链、内部 Calling tools: 协议残留与 DSML 标签，规范化代码块"""
    if not raw_answer:
        return "", ""
    text = str(raw_answer).strip()
    extracted_thought = ""

    # 1. 抽取并剥离 <think> 思考链
    think_match = re.search(r"<think>([\s\S]*?)</think>", text, re.IGNORECASE)
    if think_match:
        extracted_thought = think_match.group(1).strip()
        text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()
    elif "<think>" in text:
        parts = text.split("<think>", 1)
        text = parts[0].strip()
        extracted_thought = parts[1].strip()

    # 2. 剥离 smolagents 内部工具调用协议泄露 Calling tools: [...]
    text = re.sub(r"Calling tools:\s*\[[\s\S]*?\]\s*", "", text).strip()

    # 3. 剥离 DeepSeek 原生 DSML / XML 工具调用泄露
    text = re.sub(r"<｜｜DSML｜｜\s*calls>[\s\S]*?(?:</invoke>|$)", "", text).strip()
    text = re.sub(
        r"<parameter name=[\"\']code[\"\']>[\s\S]*?</parameter>", "", text
    ).strip()

    # 4. 规范化 <code>...</code> 标签为 Markdown ```python 代码块
    def _code_repl(m):
        c = m.group(1).strip()
        if not c:
            return ""
        return f"\n```python\n{c}\n```\n"

    text = re.sub(
        r"<code>\s*([\s\S]*?)\s*</code>", _code_repl, text, flags=re.IGNORECASE
    ).strip()

    return text.strip(), extracted_thought


def format_execution_observation(raw_obs: Any, step_num: int = 1) -> str:
    """将沙箱执行输出格式化为排版优美的结构化 Markdown"""
    empty_notice = "> ℹ️ **脚本执行成功，无标准输出**\n\n*(代码已在沙箱中正常执行完成。若需在卡片中查看接口返回或计算结果，可在脚本中调用 `print(...)` 输出)*"

    if raw_obs is None:
        return empty_notice

    text = str(raw_obs).strip()

    # 1. 清理 smolagents 冗余前缀
    if text.startswith("Print outputs:\n"):
        text = text[len("Print outputs:\n") :].strip()
    elif text.startswith("Print outputs:"):
        text = text[len("Print outputs:") :].strip()

    if not text or text == "None":
        return empty_notice

    # 2. 尝试解析为 JSON 或 Python 字典并美化输出
    formatted_data = None
    if (text.startswith("{") and text.endswith("}")) or (
        text.startswith("[") and text.endswith("]")
    ):
        try:
            parsed = json.loads(text)
            formatted_data = json.dumps(parsed, indent=2, ensure_ascii=False)
        except Exception:
            try:
                import ast

                parsed = ast.literal_eval(text)
                if isinstance(parsed, (dict, list)):
                    formatted_data = json.dumps(parsed, indent=2, ensure_ascii=False)
            except Exception as fallback_error:
                logger.debug("Fallback observation parsing failed: %s", fallback_error)

    if formatted_data:
        return f"```json\n{formatted_data}\n```"

    # 3. 错误与异常
    if any(k in text.lower() for k in ["traceback", "error:", "exception:", "failed"]):
        return f"> ⚠️ **沙箱执行告警 / 异常**\n\n```text\n{text}\n```"

    # 4. 普通终端文本
    return f"```bash\n{text}\n```"


class AgentEngine:
    def __init__(self, manager: BotManager | None = None):
        self.manager = manager or bot_manager
        self.cloud_client = UniversalCloudClient()
        self.tim_client = self.cloud_client.tim_client
        self._local = threading.local()
        # 跨轮多会话 Python 沙箱环境变量存储 (按 bot_id:user 隔离)。
        # OrderedDict lets reads refresh recency and evicts the oldest state
        # when the bounded cache reaches its limit.
        self._bot_sandbox_states: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._sandbox_max_entries = DEFAULT_SANDBOX_MAX_ENTRIES
        self._sandbox_lock = threading.Lock()

    def _sandbox_key(
        self,
        bot_id: str,
        user_id: str | None = None,
        issuer: str | None = None,
        tenant_id: str | None = None,
    ) -> str:
        active_scope = runtime_auth_scope.get()
        effective_user = user_id or active_scope.subject or runtime_user_id.get()
        effective_issuer = active_scope.issuer if issuer is None else str(issuer or "")
        effective_tenant = active_scope.tenant_id if tenant_id is None else str(tenant_id or "")
        if not effective_issuer and not effective_tenant:
            return f"{bot_id}:{effective_user}"
        import hashlib

        identity = "\0".join(
            (str(bot_id), effective_issuer, effective_tenant, effective_user)
        )
        return f"{bot_id}:scope:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"

    def _remember_sandbox(self, key: str, value: dict[str, Any]) -> None:
        with self._sandbox_lock:
            # Re-inserting an existing key makes it the most recently used
            # entry, so an active conversation is retained over an idle one.
            self._bot_sandbox_states.pop(key, None)
            self._bot_sandbox_states[key] = value
            while len(self._bot_sandbox_states) > self._sandbox_max_entries:
                self._bot_sandbox_states.popitem(last=False)

    def _get_sandbox(self, key: str) -> dict[str, Any]:
        with self._sandbox_lock:
            state = self._bot_sandbox_states.get(key)
            if state is not None:
                self._bot_sandbox_states.move_to_end(key)
            return dict(state) if state else {}

    def clear_sandbox(self, bot_id: str, user_id: str | None = None):
        """清空指定 BOT 与用户的跨轮沙箱变量缓存"""
        with self._sandbox_lock:
            self._bot_sandbox_states.pop(self._sandbox_key(bot_id, user_id), None)
            self._bot_sandbox_states.pop(bot_id, None)  # legacy single-user key

    def clear_bot_sandboxes(self, bot_id: str) -> None:
        """删除 BOT 时清掉该 BOT 的全部用户沙箱，避免其他用户的状态残留。"""
        prefix = f"{bot_id}:"
        with self._sandbox_lock:
            stale_keys = [
                key for key in self._bot_sandbox_states
                if key == bot_id or key.startswith(prefix)
            ]
            for key in stale_keys:
                self._bot_sandbox_states.pop(key, None)

    def get_effective_llm_config(self, bot: BotConfig) -> LLMConfig:
        """获取 BOT 生效的 LLM 配置（BOT 级别覆盖 > 全局配置）"""
        global_cfg = self.manager.get_llm_config()
        if bot.model_override:
            # 合并覆盖
            data = global_cfg.model_dump()
            data.update(
                {
                    k: v
                    for k, v in bot.model_override.items()
                    if v is not None and v != ""
                }
            )
            return LLMConfig(**data)
        return global_cfg

    def create_model(self, llm_cfg: LLMConfig) -> OpenAIServerModel:
        """根据配置实例化 smolagents OpenAIServerModel"""
        provider = (llm_cfg.provider or "deepseek").lower()
        provider_default = PROVIDER_DEFAULTS.get(provider, {})

        api_key = (llm_cfg.api_key or "").strip()
        base_url = (llm_cfg.base_url or "").strip() or provider_default.get("base_url")
        model_name = (llm_cfg.model_name or "").strip() or provider_default.get(
            "default_model", "deepseek-chat"
        )

        # 出站前统一走共享 SSRF 守卫（含历史存量配置的兜底校验）
        if base_url:
            from core.agent.model_url_guard import (
                UnsafeModelUrlError,
                validate_model_base_url,
            )

            try:
                base_url = validate_model_base_url(base_url)
            except UnsafeModelUrlError as exc:
                raise ValueError(f"模型 Base URL 被安全策略拒绝：{exc}") from exc

        # 如果没有配置 API Key 且是本地 ollama，填默认占位
        if not api_key and (
            provider == "ollama" or (base_url and "localhost" in base_url)
        ):
            api_key = "ollama"

        if not api_key:
            raise ValueError(
                f"大模型 API Key 尚未配置。请在 TC Studio 的 AI 智能体设置中配置 [{llm_cfg.provider}] 的 API Key 或接入 Ollama 本地模型。"
            )

        # 归一化 base_url
        if base_url:
            base_url = base_url.rstrip("/")
            if provider == "ollama" and not base_url.endswith("/v1"):
                base_url = f"{base_url}/v1"
            elif (
                provider == "qwen"
                and not base_url.endswith("/v1")
                and "aliyuncs.com" in base_url
            ):
                base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

        return RobustStreamingOpenAIServerModel(
            model_id=model_name,
            api_base=base_url,
            api_key=api_key,
            temperature=llm_cfg.temperature,
        )

    def assemble_system_prompt(
        self,
        bot: BotConfig,
        user_id: str | None = None,
        is_admin: bool | None = None,
    ) -> str:
        """组装动态 System Prompt：基础人设 + 自我增强准则 + 长期记忆 + 技能清单"""
        prompt_parts = []
        effective_user_id = user_id or runtime_user_id.get()
        effective_is_admin = runtime_is_admin.get() if is_admin is None else is_admin

        # 1. 基础人设
        prompt_parts.append(f"# 角色设定\n{bot.base_prompt}\n")

        # 2. 腾讯云多业务架构与 API 调度原则（动态自适应：从 registry SSoT 自动提取，彻底拒绝硬编码）
        from ..registry import registry

        services_summary = registry.get_services_summary()

        prompt_parts.append(
            "## 核心使命与工具调度原则\n"
            f"1. 你拥有直接调度腾讯云多业务全量接口的最高权限：完整涵盖{services_summary['prompt_text']}。\n"
            "   - 【统一调度工具】: `cloud_call(action_or_path, payload)`（如 Action `DescribeLiveStreamState` 或 IM 路径 `openim/sendmsg`）；\n"
            "   - 【统一检索与文档】: `cloud_search_apis(keyword, service)`（全量跨云搜索或按 service 筛选）与 `cloud_get_doc(action_or_path)`（提取字段规范、复杂结构体及入参样例）；\n"
            "   - 【向后兼容】: 自动兼容旧版指令 `tim_call`、`tim_search_apis`、`tim_get_doc`。\n"
            "2. Python 沙箱原生环境已为你内置了快捷函数与 SDK 驱动：\n"
            "   - `cloud_call(action_or_path, payload, service=None)`：统一调度网关；\n"
            "   - `live_call(action, payload=None)`：云直播 API 3.0 原生快捷调用（例如 `live_call('DescribeLiveStreamState', {'DomainName': '...'})`）；\n"
            "   - `gen_live_push_url(domain, stream_name, key=None)` / `gen_live_play_url(...)`：快捷生成带防盗链签名的多协议推拉流地址；\n"
            "   - `im_call(path, payload=None)` / `tim_call(path, payload=None)`：即时通信 IM REST 原生快捷调用；\n"
            "   - `search_apis(keyword, service='all')` / `get_doc(action_or_path)`：沙箱内即时查接口与文档结构；\n"
            "   - 所有云调用必须经由 `cloud_call` / `{service}_call`，沙箱不会暴露底层客户端或签名凭据。\n"
            "3. 当需要执行批量操作（如批量禁言、循环撤回、多流巡检、违规推流熔断清理、拉取多页群成员并清洗过滤）时，请直接编写并执行 Python 脚本，使用 for 循环和条件判断高效完成。\n"
            "4. 编写 Python 脚本时，首行建议使用中文注释简短写出本步行动意图（例如 `# 1. 批量巡检活跃直播流推流状态` 或 `# 1. 获取群成员列表与禁言状态`），以便系统和用户直观追踪步骤进展。\n"
            "5. 输出打印规范（重要）：调用接口或完成数据统计后，请务必使用 `print(...)` 打印出接口返回的关键响应或核心数据结果，以便在前端卡片中直观展现 API 响应详情与运行日志。\n"
            "6. 保持回答专业、精炼、客观，并在调用关键接口或修改系统配置后，明确汇总操作结果和受影响的业务实体（直播流/域名/群组/账号）。\n"
            "7. 高危破坏性操作安全防线：当涉及直播断流与禁推 (`DropLiveStream`, `ForbidLiveStream`, `DeleteLiveRecordRule`, `DeleteLiveTranscodeRule`)、解散群组 (`destroy_group`)、注销/删除账号 (`account_delete`)、清空漫游消息 (`clean_roam_msg`) 或批量踢人时，必须在回答中首先向用户清晰列出受影响的目标流名、域名、群 ID、账号范围以及潜在不可逆后果，并要求二次确认，严禁未经告知即大范围批量销毁生产数据。\n"
            "8. 高效批量执行与收敛准则（关键）：\n"
            "   - 在处理复杂任务（如「拉取 TOP5 群并拉取各自最近10条消息」或「扫描在线直播流并对帧率异常流做统计告警」）时，应在一个完整的 Python 脚本中通过循环和函数紧凑完成，严禁将单一流程拆分为过多单行探索脚本，以免耗尽步数配额；\n"
            "   - 当已通过 print 获得核心数据后，必须在脚本最后调用 `final_answer(markdown_report)` 直接输出完整的结构化业务报告。\n"
            "9. 数据结构防御式编程规范：\n"
            "   - 【IM 消息体兼容】：在拉取历史消息时（如 `group_msg_get_simple`），普通聊天消息的 `MsgBody` 是列表 (`list`)，但在群组系统通知/事件中部分群类型返回字典 (`dict`) 或空；务必同时兼容 `isinstance(body, list)` 与 `isinstance(body, dict)`；\n"
            "   - 【Live API 3.0 应答规范】：云直播 API 3.0 应答标准结构为 `Response: { ... , RequestId: '...' }`，在脚本中解析时优先读取 `res.get('Response', res)`，并做好空键防御。\n\n"
            "## 📁 产出物与生成文件规范 (File Generation)\n"
            "若任务需要产出清洗后的数据、导出报表（Excel/CSV）、统计表格、生成的日志或图表数据：\n"
            "1. 【推荐】直接调用内置辅助函数 `save_file(filename, data)`（支持字典、列表、字节与文本），自动保存至智能体专属文件中心；\n"
            "2. 文件解析统一交给 `read_file`，Python 沙箱不提供第三方文件解析库或宿主机绝对路径访问；\n"
            f"3. 【会话下载链接规范】（重要）：在通过 `final_answer(...)` 输出最终回答汇报给用户时，只要本次任务生成了文件，必须在 Markdown 回答中为用户提供直接下载链接，格式规范如下：\n"
            f"   - `[📥 点击直接下载 文件名](/api/agent/bots/{bot.id}/files/文件名?download=true)`\n"
            "4. 系统会在会话界面自动为你生成的产出物渲染专属的一键下载卡片，并即时同步至右侧抽屉「产出物与文件中心」。\n"
        )

        # 3. 动态注入自增强的行为准则 (Prompt Self-Enhancement)
        if bot.learned_guidelines:
            guidelines_text = "\n".join(
                [
                    f"- **[准则 {idx + 1}]**: {g.instruction}"
                    + (f" （背景: {g.reason}）" if g.reason else "")
                    for idx, g in enumerate(bot.learned_guidelines)
                ]
            )
            prompt_parts.append(
                f"## 🛡️ 智能体自我强化的行为准则 (Self-Enhanced Guidelines)\n"
                f"这些是你或用户在过去的实践中沉淀的专属业务规则，你必须严格优先遵守：\n{guidelines_text}\n"
            )

        # 4. 动态注入长期记忆 (Long-term Memories)
        memories = self.manager.list_memories(bot.id)
        if memories:
            max_injected = 15
            displayed_mem = memories[:max_injected]
            mem_text = "\n".join([f"- `{m.key}`: {m.content}" for m in displayed_mem])
            extra_hint = (
                f"\n*(已注入最近 {max_injected} 条核心事实，其余更早记忆可自主调用 `recall_memory(keyword)` 进行定向召回)*"
                if len(memories) > max_injected
                else ""
            )
            prompt_parts.append(
                f"## 🧠 智能体长期事实记忆 (Long-Term Memories)\n"
                f"已记住的关键业务事实与用户偏好：\n{mem_text}{extra_hint}\n"
            )

        # 5. 动态注入已保存技能 (Skills)
        skills = self.manager.list_skills(bot.id)
        if skills:
            skills_text = "\n".join(
                [
                    f"- `{s.name}`: {s.description} (入参: {json.dumps(s.input_schema, ensure_ascii=False)})"
                    for s in skills
                ]
            )
            prompt_parts.append(
                f"## ⚡ 智能体已沉淀的专属技能库 (Available Skills)\n"
                f"你可以直接通过 `run_skill(name, kwargs)` 运行这些技能，也可以在解决新场景后使用 `save_skill` 沉淀新技能：\n{skills_text}\n"
            )

        # 6. 自主进化与无感沉淀机制（严禁在对话中打扰用户）
        prompt_parts.append(
            "## 💡 自主进化与无感沉淀机制（对用户完全无感）\n"
            "1. **对用户无感沉淀（核心原则）**：无论沉淀技能、记录长期事实记忆还是提炼行为准则，对用户来说都应该是**完全自然、无感、流畅**的。\n"
            "   - **绝对禁止**在给用户的回复中询问“是否需要沉淀为技能”、“是否记录为记忆”等确认语或提示框。\n"
            "   - 用户无需分心做任何维护决策，由你在解决问题和对话过程中自主决策、自主进化。\n"
            "2. **自主沉淀技能 (save_skill)**：当你编写并成功执行了一个具备高复用价值的复杂业务流（多步 API 串联、批量数据清洗与操作），请直接自主调用 `save_skill` 工具将其保存到技能库，供日后直接调用。\n"
            "3. **自主自我增强提示词 (enhance_self_prompt)**：当用户在交流中表达了长期习惯、操作偏好、默认规则或约束（例如“禁言默认1小时”、“处理群消息前先打印日志”），请在思考或执行时自主调用 `enhance_self_prompt` 工具将其固化为行为准则，无需向用户索要确认。\n"
            "4. **自主事实记忆记录 (save_memory)**：当用户提及了关键实体标识（群 ID、特定管理员、环境配置、常用关键词等），请自主调用 `save_memory` 工具将其存入长期记忆。\n"
            "5. **回复精炼专注**：给用户的最终回答只需清晰告知任务执行结果或解答用户的问题，无需赘述你私下沉淀了什么技能或准则，保持极简专业。\n"
        )

        # 7. 内建全能助手独享的孵化与创建智能体特权 (自然语言即刻触发)
        if bot.id == "default_assistant":
            prompt_parts.append(
                "## 🤖 智能体孵化与创建专享特权 (create_bot)\n"
                "作为系统内建唯一的腾讯云全能架构管家，你独享通过 `create_bot` 工具为用户创建新专属业务智能体的特权。\n"
                "当用户以**任何自然语言**提出创建、新增、定制新智能体的需求时（例如“帮我建一个社群风控官”、“建一个自动查单号的机器人”、“孵化一个消息巡检助手”，**无需任何特殊命令或前缀**）：\n"
                "1. 充分分析用户诉求，智能推断其名称、头像 Emoji、专业业务定位和约 200 字专业系统设定提示词（强调严谨技术人设，结合全量云接口与沙箱能力）；\n"
                "2. 自主调用 `create_bot(name, goal, avatar, base_prompt, suggested_prompts)` 工具即刻生成该智能体；\n"
                "3. 创建成功后，在回复中清晰展示新智能体的名称、头像、职责和推荐指令，告知用户该智能体已就绪，可在左侧切换或在会话中随时进行协同指挥调度。\n"
            )

        # 8. 团队智能体协同网络与自然语言无感调度 (无需强制 @)
        other_bots = [
            b
            for b in self.manager.list_bots(user_id=effective_user_id)
            if b.get("id") != bot.id
            and (
                effective_is_admin
                or b.get("visibility") == "public"
                or b.get("owner_id") == effective_user_id
            )
        ]
        if other_bots:
            roster_lines = [
                f"- {b.get('avatar', '🤖')} **【{b.get('name')}】** (ID: `{b.get('id')}`): {b.get('description') or '协助处理专业 IM 业务需求'}"
                for b in other_bots
            ]
            roster_text = "\n".join(roster_lines)
            prompt_parts.append(
                f"## 🤝 团队智能体协同网络与自然语言无感调度\n"
                f"当前工作室入驻了以下专业领域的智能体伙伴：\n{roster_text}\n\n"
                "### 🎯 自然语言无感协同准则（支持自然语言任意唤起，绝不强制依赖 @ 符号）：\n"
                "1. **免 @ 的自然语言调度**：当用户在口语中提及智能体名称、简称或角色（例如“让风控官查一下违规”、“叫巡检员看下网络延时”、“请消息管理员批量撤回”等，**无论是否带有 @ 符号**），你必须敏锐识别协同意图，主动调用 `call_agent(agent_name, task)` 将子任务指挥委派给对应伙伴智能体。\n"
                "2. **专业职责自适应协同**：当用户提出的复杂需求涉及团队中某位专职智能体（例如群风控、深度巡检、研报分发等），即使用户没有显式指定名字，你也可以主动调用 `call_agent` 协同专家伙伴，共同推进任务。\n"
                "3. **协同结论融合汇报**：收到目标智能体的执行反馈后，将其专业结论自然地整合到你的最终回答中（例如：“已联动【社群风控官】完成排查，反馈如下：...”），保持专业严谨、一体化的团队作战风格。\n"
            )
        else:
            prompt_parts.append(
                "## 🤝 智能体多 Agent 协同机制\n"
                "你具备与其他智能体协同作业的能力。当团队中有其他专业智能体加入时，你可以通过自然语言识别意图或 `call_agent` 工具调度他们协同处理复杂业务需求（支持自然语言无感识别，不强制依赖 @ 符号）。\n"
            )

        # 9. 附件与多格式数据处理指引
        prompt_parts.append(
            "## 📁 附件文档与多格式数据处理能力 (File & Data Processing)\n"
            "1. 你具备全能的文件与文档处理能力，支持用户上传的 Word (DOCX)、Excel (XLSX/XLS)、CSV、TXT、JSON、代码、PDF、图像等多种格式附件。\n"
            "2. 当用户上传了附件时：\n"
            "   - 你可以使用内置工具 `read_file(file_path)` 读取文档、代码或文本内容；\n"
            "   - 文件内容由服务端在 BOT 上传目录内解析并限制读取范围；如需进一步处理，请使用 `read_file` 获取内容后在内存中分析；\n"
            "   - 若附件涉及接口调用参数或业务实体数据，请将其与腾讯云多业务全量接口无缝串联自动化执行。\n"
        )

        # 10. 数据统计就地可视化图表输出 (Interactive ECharts)
        prompt_parts.append(
            "## 📊 数据统计与就地可视化图表输出 (Interactive ECharts Visualization)\n"
            "当你通过 Python 脚本或 IM API 统计出时序趋势、群活跃度、违规消息类型占比、成员分布、错误码分布等数据分析结果时，除了文字与 Markdown 表格总结外，**请在回答中直接输出 ```echarts 代码块**，前端会自动就地渲染为高科技交互式折线图、柱状图或饼图！\n"
            "支持以下两种格式（任选其一）：\n"
            "1. 极简推荐配置（自动适配深浅主题与响应式）：\n"
            "```echarts\n"
            "{\n"
            '  "type": "bar", // 或 "line", "pie"\n'
            '  "title": "近7天各群活跃消息数",\n'
            '  "labels": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"],\n'
            '  "data": [120, 192, 150, 240, 310, 420, 380]\n'
            "}\n"
            "```\n"
            "2. 官方完整 ECharts option 配置（支持 tooltip, legend, series 等自定义高级视觉）：\n"
            "```echarts\n"
            "{\n"
            '  "title": { "text": "违规消息类型占比" },\n'
            '  "tooltip": { "trigger": "item" },\n'
            '  "series": [{\n'
            '    "type": "pie",\n'
            '    "radius": ["40%", "70%"],\n'
            '    "data": [\n'
            '      { "name": "广告垃圾", "value": 140 },\n'
            '      { "name": "涉黄低俗", "value": 35 },\n'
            '      { "name": "恶意刷屏", "value": 85 }\n'
            "    ]\n"
            "  }]\n"
            "}\n"
            "```\n"
            "在 Python 脚本中，你也可以直接调用内置函数 `print(render_chart('line', '标题', labels, data))` 输出图表代码块。\n"
        )

        # 11. 企业知识库与私有运维手册规范
        if getattr(bot, "visibility", "private") != "public":
            from .rag_manager import rag_manager

            docs = rag_manager.list_documents(bot.id)
            enabled_docs = [d for d in docs if d.get("enabled", True)]
            if enabled_docs:
                doc_names = ", ".join([f"《{d['name']}》" for d in enabled_docs])
                prompt_parts.append(
                    f"## 📚 企业私有知识库与运维手册已挂载 ({len(enabled_docs)} 份文档)\n"
                    f"当前智能体已挂载以下内部业务知识文档：{doc_names}。\n"
                    f"当用户提问涉及团队特有排障规范、错误码处置 SOP、私有群规或非公开业务规则时，请优先调用 `search_knowledge(query)` 工具进行检索核实，确保回答完全符合团队私有业务规范。\n"
                )

        return "\n".join(prompt_parts)

    def record_orchestration(self, record: dict[str, Any]):
        """记录一条多智能体协同调用事件（支持线程隔离）"""
        self.manager.record_orchestration(record)
        active_list = getattr(self._local, "active_stream_orchestrations", None)
        if active_list is not None and isinstance(active_list, list):
            active_list.append(record)

    def execute_subtask(
        self,
        caller_bot_id: str,
        target_bot_id: str,
        task: str,
        call_chain: list[str] | None = None,
        approval_requester: Any | None = None,
        orchestration_recorder: Any | None = None,
    ) -> str:
        """为协同调度执行子任务 (同步返回目标智能体的最终输出)"""
        # Normalize the delegation path at the engine boundary.  Tool objects
        # keep an immutable snapshot and callers cannot mutate the recursion
        # guard between validation and child-agent creation.
        chain = tuple(call_chain or (caller_bot_id,))
        if target_bot_id in chain:
            return f"⚠️ 检测到循环协同调用（{' -> '.join(chain)} -> {target_bot_id}），已自动阻断以避免死锁。"

        target_bot = self.manager.get_bot(target_bot_id)
        if not target_bot:
            raise ValueError(f"目标智能体 '{target_bot_id}' 不存在")

        caller_bot = self.manager.get_bot(caller_bot_id)
        active_scope = runtime_auth_scope.get()
        if not self.manager.bot_scope_matches(
            target_bot,
            issuer=active_scope.issuer,
            tenant_id=active_scope.tenant_id,
        ):
            raise PermissionError("目标智能体不属于当前认证范围")
        if (
            not caller_bot
            or not self.manager.bot_scope_matches(
                caller_bot,
                issuer=active_scope.issuer,
                tenant_id=active_scope.tenant_id,
            )
            or not (
                runtime_is_admin.get()
                or caller_bot.visibility == "public"
                or caller_bot.owner_id == runtime_user_id.get()
            )
        ):
            raise PermissionError("当前智能体不属于当前认证范围或无权发起协同")
        if not (
            runtime_is_admin.get()
            or target_bot.visibility == "public"
            or target_bot.owner_id == runtime_user_id.get()
        ):
            raise PermissionError("无权委派到目标智能体")
        caller_name = caller_bot.name if caller_bot else caller_bot_id

        # 记录到目标智能体历史中
        self.manager.append_history(
            target_bot_id,
            "user",
            f"【来自智能体 @{caller_name} 的协同委派任务】: {task}",
        )

        llm_cfg = self.get_effective_llm_config(target_bot)
        model = self.create_model(llm_cfg)
        new_chain = chain + (target_bot_id,)
        delegated_approval_requester = approval_requester
        if approval_requester:

            def delegated_approval_request(request: dict):
                enriched = dict(request)
                enriched.update(
                    {
                        "caller_bot_id": caller_bot_id,
                        "caller_bot_name": caller_name,
                        "target_bot_id": target_bot_id,
                        "target_bot_name": target_bot.name,
                        "delegation_chain": list(new_chain),
                    }
                )
                return approval_requester(enriched)

            delegated_approval_requester = delegated_approval_request

        executor = _create_code_executor(
            AUTHORIZED_IMPORTS,
            max_print_outputs_length=DEFAULT_MAX_PRINT_OUTPUTS_LENGTH,
            timeout_seconds=DEFAULT_CODE_EXECUTION_TIMEOUT_SECONDS,
        )
        delegated_tool_approval_requester = executor.approval_requester(
            delegated_approval_requester
        )
        delegated_tool_orchestration_recorder = executor.orchestration_recorder(
            orchestration_recorder
        )

        tools = build_bot_tools(
            target_bot_id,
            self.manager,
            self.tim_client,
            engine=self,
            call_chain=list(new_chain),
            approval_requester=delegated_tool_approval_requester,
            cloud_client=self.cloud_client,
            orchestration_recorder=delegated_tool_orchestration_recorder,
            include_knowledge_tool=getattr(target_bot, "visibility", "private") != "public",
        )
        system_prompt = self.assemble_system_prompt(target_bot)

        task_prompt = (
            f"{system_prompt}\n\n"
            f"【协同调度任务】:\n"
            f"你正作为专业智能体接受来自智能体 @{caller_name} 的协同任务委托。\n"
            f"需求内容: {task}\n"
            f"请充分发挥你的专业定位与专属工具，高效完成该需求并给出结构化结论与关键数据。"
        )

        agent = CodeAgent(
            tools=tools,
            model=model,
            additional_authorized_imports=AUTHORIZED_IMPORTS,
            max_print_outputs_length=DEFAULT_MAX_PRINT_OUTPUTS_LENGTH,
            max_steps=DEFAULT_SUBTASK_MAX_STEPS,
            executor=executor,
        )

        # 注入目标智能体沙箱环境与产出物工具
        target_uploads_dir = os.path.join(
            self.manager._runtime_dir(target_bot_id), "uploads"
        )
        os.makedirs(  # pi-lens-ignore: unchecked-throwing-call-python -- sandbox file capability
            target_uploads_dir, exist_ok=True
        )

        def subtask_save_file(filename: str, data: Any) -> dict[str, Any]:
            safe_name = os.path.basename(filename)
            dest_path = os.path.join(target_uploads_dir, safe_name)
            temp_path = _asset_temp_path(dest_path)
            if hasattr(data, "to_excel") and safe_name.endswith((".xlsx", ".xls")):
                data.to_excel(temp_path, index=False)
            elif hasattr(data, "to_csv") and safe_name.endswith(".csv"):
                data.to_csv(temp_path, index=False)
            elif isinstance(data, (bytes, bytearray)):
                with (
                    open(temp_path, "wb") as f  # pi-lens-ignore: unchecked-throwing-call-python -- sandbox file capability
                ):
                    f.write(data)
            elif isinstance(data, str):
                with (
                    open(temp_path, "w", encoding="utf-8") as f  # pi-lens-ignore: unchecked-throwing-call-python -- sandbox file capability
                ):
                    f.write(data)
            elif isinstance(data, (dict, list)):
                with (
                    open(temp_path, "w", encoding="utf-8") as f  # pi-lens-ignore: unchecked-throwing-call-python -- sandbox file capability
                ):
                    json.dump(data, f, ensure_ascii=False, indent=2)
            else:
                with (
                    open(temp_path, "w", encoding="utf-8") as f  # pi-lens-ignore: unchecked-throwing-call-python -- sandbox file capability
                ):
                    f.write(str(data))
            with self.manager._lock:
                _commit_generated_asset(temp_path, dest_path, target_uploads_dir)
            return {
                "filename": safe_name,
                "path": dest_path,
                "url": f"/api/agent/bots/{target_bot_id}/files/{safe_name}",
            }

        def subtask_bot_open(file, mode="r", *args, **kwargs):
            return _confined_open(target_uploads_dir, file, mode, *args, **kwargs)

        subtask_cloud_call = next(
            (t for t in tools if isinstance(t, CloudCallTool)), None
        ) or CloudCallTool(
            bot_manager=self.manager,
            tim_client=self.tim_client,
            approval_requester=delegated_tool_approval_requester,
        )
        subtask_search_tool = (
            next((t for t in tools if isinstance(t, CloudSearchApisTool)), None)
            or CloudSearchApisTool()
        )
        subtask_doc_tool = (
            next((t for t in tools if isinstance(t, CloudGetDocTool)), None)
            or CloudGetDocTool()
        )

        _install_sandbox_scope(
            agent,
            {
                "open": subtask_bot_open,
                "render_chart": render_chart,
                "save_file": subtask_save_file,
                "OUTPUT_DIR": target_uploads_dir,
                "WORKSPACE_DIR": target_uploads_dir,
                **build_sandbox_scope(
                    caller_tool=subtask_cloud_call,
                    search_tool=subtask_search_tool,
                    doc_tool=subtask_doc_tool,
                    cloud_client=self.cloud_client,
                    tim_client=self.tim_client,
                ),
            },
        )

        try:
            resp = agent.run(task_prompt)
            final_answer = str(resp) if resp is not None else "任务已处理。"
            cleaned_ans, _ = clean_final_answer(final_answer)

            # 存入目标智能体历史
            self.manager.append_history(
                target_bot_id,
                "assistant",
                cleaned_ans or "协同任务已执行完毕。",
                metadata={"delegated_by": caller_bot_id},
            )

            return cleaned_ans or "协同任务已完成。"
        finally:
            executor.close()

    def run_agent_stream(
        self,
        bot_id: str,
        user_message: str,
        attachments: list[dict[str, Any]] | None = None,
        skip_user_append: bool = False,
        stop_checker: Any | None = None,
        approval_requester: Any | None = None,
        execution_controller: Any | None = None,
    ):
        """
        生成器：执行 CodeAgent 并按结构流式产生事件 (Thought, Code, Output, Message)
        """
        bot = self.manager.get_bot(bot_id)
        if not bot:
            yield {"event": "error", "data": {"error": f"BOT '{bot_id}' 不存在"}}
            return

        # 1. 保存用户提问与附件元数据到历史 (如果是重新生成，则复用原有用户提问，跳过追加)
        if not skip_user_append:
            user_meta = {}
            if attachments:
                clean_attachments = []
                for att in attachments:
                    if isinstance(att, dict):
                        c = dict(att)
                        c.pop("data_url", None)
                        clean_attachments.append(c)
                    else:
                        clean_attachments.append(att)
                user_meta["attachments"] = clean_attachments
            self.manager.append_history(
                bot_id, "user", user_message, metadata=user_meta
            )

        # 2. 检查大模型配置
        llm_cfg = self.get_effective_llm_config(bot)
        try:
            model = self.create_model(llm_cfg)
        except Exception as e:
            err_msg = str(e)
            yield {"event": "error", "data": {"error": err_msg}}
            self.manager.append_history(bot_id, "assistant", f"[配置错误] {err_msg}")
            return

        # 3. 组装 Tools & Agent.  Code execution is delegated to a killable
        # process; the approval callback is proxied back to this process.
        executor = _create_code_executor(
            AUTHORIZED_IMPORTS,
            max_print_outputs_length=DEFAULT_MAX_PRINT_OUTPUTS_LENGTH,
            timeout_seconds=DEFAULT_CODE_EXECUTION_TIMEOUT_SECONDS,
        )
        tool_approval_requester = executor.approval_requester(approval_requester)
        tool_orchestration_recorder = executor.orchestration_recorder(
            self.record_orchestration
        )
        tools = build_bot_tools(
            bot_id,
            self.manager,
            self.tim_client,
            engine=self,
            approval_requester=tool_approval_requester,
            cloud_client=self.cloud_client,
            orchestration_recorder=tool_orchestration_recorder,
            include_knowledge_tool=getattr(bot, "visibility", "private") != "public",
        )
        system_prompt = self.assemble_system_prompt(bot)
        stream_start_time = time.time()

        yield {
            "event": "start",
            "data": {
                "bot_id": bot.id,
                "bot_name": bot.name,
                "model": llm_cfg.model_name,
                "provider": llm_cfg.provider,
                "timestamp": stream_start_time,
                "attachments_count": len(attachments or []),
            },
        }

        effective_max_steps = (
            bot.max_steps
            or getattr(llm_cfg, "max_steps", None)
            or DEFAULT_STREAM_MAX_STEPS
        )

        agent = CodeAgent(
            tools=tools,
            model=model,
            additional_authorized_imports=AUTHORIZED_IMPORTS,
            max_print_outputs_length=DEFAULT_MAX_PRINT_OUTPUTS_LENGTH,
            max_steps=effective_max_steps,
            stream_outputs=True,
            executor=executor,
        )

        if execution_controller:
            execution_controller(executor.terminate)
        if stop_checker and stop_checker():
            executor.close()
            return

        # 挂载跨轮已留存的沙箱变量环境
        saved_state = self._get_sandbox(self._sandbox_key(bot_id))
        if saved_state:
            try:
                agent.state.update(saved_state)
            except Exception as e:
                logger.warning(
                    "Failed to restore sandbox state for %s: %s", bot_id, str(e)
                )

        # 定制 final_answer 兜底 Prompt 模板，严禁输出代码和内部调用标记
        if (
            hasattr(agent, "prompt_templates")
            and "final_answer" in agent.prompt_templates
        ):
            agent.prompt_templates["final_answer"]["pre_messages"] = (
                "智能体在执行任务时达到了步数上限。请你作为资深架构师和业务助手，基于前面各步骤已收集到的所有数据、输出和日志（Observation），直接向用户给出最终的结构化 Markdown 业务汇报。\n"
                "【严格禁令与输出格式规范】：\n"
                "1. 严禁输出任何 Python 代码块、严禁使用 <code> 标签、严禁输出 ```python 继续写脚本；\n"
                "2. 严禁输出任何 'Calling tools:' 等系统内部工具调用内容；\n"
                "3. 必须直接使用专业清晰的 Markdown 格式（表格、列表、要点汇总）向用户呈现已获取到的结果；\n"
                "4. 若因步数限制导致部分数据尚未拉全，请如实向用户汇总已获取部分，并给出后续指引。"
            )
            agent.prompt_templates["final_answer"]["post_messages"] = (
                "请基于上述全部执行记录与数据结果，直接向用户给出针对需求「{{task}}」的最终 Markdown 专业报告（切记：只输出面向用户的文字/表格结论，绝不要输出代码）："
            )

        # 注入当前智能体专属产出物目录与资产中心工具
        uploads_dir = os.path.join(self.manager._runtime_dir(bot_id), "uploads")
        os.makedirs(  # pi-lens-ignore: unchecked-throwing-call-python -- sandbox file capability
            uploads_dir, exist_ok=True
        )

        run_start_time = time.time()
        initial_uploads_files = (
            set(os.listdir(uploads_dir)) if os.path.exists(uploads_dir) else set()  # pi-lens-ignore: unchecked-throwing-call-python -- sandbox file capability
        )
        tracked_output_files: list[dict[str, Any]] = []

        def sync_generated_artifacts():
            """归集当前用户 uploads 目录内本次运行产生的文件，不扫描共享工作目录。"""
            try:
                if os.path.exists(uploads_dir):
                    for fn in os.listdir(uploads_dir):
                        fp = os.path.join(uploads_dir, fn)
                        if not os.path.isfile(fp):
                            continue
                        stat = os.stat(fp)
                        mtime = stat.st_mtime
                        if (
                            fn not in initial_uploads_files
                            or mtime >= (run_start_time - 2.0)
                        ) and not any(
                            f["name"] == fn or f.get("saved_name") == fn
                            for f in tracked_output_files
                        ):
                            cat, ftype = get_file_category_and_type(fn)
                            orig_name = re.sub(r"^f_\d+_", "", fn)
                            tracked_output_files.append(
                                {
                                    "id": f"att-{int(time.time() * 1000)}-{len(tracked_output_files)}",
                                    "name": orig_name,
                                    "saved_name": fn,
                                    "size": stat.st_size,
                                    "size_formatted": format_file_size(stat.st_size),
                                    "category": cat,
                                    "type": ftype,
                                    "path": fp,
                                    "url": f"/api/agent/bots/{bot_id}/files/{fn}",
                                    "download_url": f"/api/agent/bots/{bot_id}/files/{fn}?download=true",
                                    "created_at": datetime.fromtimestamp(
                                        mtime, tz=UTC
                                    ).isoformat(),
                                }
                            )
            except Exception as ex:
                logger.warning(
                    "Error scanning uploads in sync_generated_artifacts: %s", ex
                )

        def save_file(filename: str, data: Any) -> dict[str, Any]:
            """保存文件至当前智能体专属资产中心，自动支持字符串、字节、JSON字典、Pandas DataFrame"""
            safe_name = os.path.basename(filename)
            dest_path = os.path.join(uploads_dir, safe_name)
            temp_path = _asset_temp_path(dest_path)
            if hasattr(data, "to_excel") and safe_name.endswith((".xlsx", ".xls")):
                data.to_excel(temp_path, index=False)
            elif hasattr(data, "to_csv") and safe_name.endswith(".csv"):
                data.to_csv(temp_path, index=False)
            elif isinstance(data, (bytes, bytearray)):
                with (
                    open(temp_path, "wb") as f  # pi-lens-ignore: unchecked-throwing-call-python -- sandbox file capability
                ):
                    f.write(data)
            elif isinstance(data, str):
                with (
                    open(temp_path, "w", encoding="utf-8") as f  # pi-lens-ignore: unchecked-throwing-call-python -- sandbox file capability
                ):
                    f.write(data)
            elif isinstance(data, (dict, list)):
                with (
                    open(temp_path, "w", encoding="utf-8") as f  # pi-lens-ignore: unchecked-throwing-call-python -- sandbox file capability
                ):
                    json.dump(data, f, ensure_ascii=False, indent=2)
            else:
                with (
                    open(temp_path, "w", encoding="utf-8") as f  # pi-lens-ignore: unchecked-throwing-call-python -- sandbox file capability
                ):
                    f.write(str(data))

            with self.manager._lock:
                _commit_generated_asset(temp_path, dest_path, uploads_dir)
            file_size = os.path.getsize(dest_path)
            cat, ftype = get_file_category_and_type(safe_name)
            download_url = f"/api/agent/bots/{bot_id}/files/{safe_name}?download=true"
            preview_url = f"/api/agent/bots/{bot_id}/files/{safe_name}"

            if not any(
                f["name"] == safe_name or f.get("saved_name") == safe_name
                for f in tracked_output_files
            ):
                tracked_output_files.append(
                    {
                        "id": f"att-{int(time.time() * 1000)}-{len(tracked_output_files)}",
                        "name": safe_name,
                        "saved_name": safe_name,
                        "size": file_size,
                        "size_formatted": format_file_size(file_size),
                        "category": cat,
                        "type": ftype,
                        "path": dest_path,
                        "url": preview_url,
                        "download_url": download_url,
                        "created_at": datetime.now(UTC).isoformat(),
                    }
                )

            return {
                "filename": safe_name,
                "path": dest_path,
                "size": file_size,
                "url": preview_url,
                "download_url": download_url,
                "message": f"文件「{safe_name}」已成功保存至资产中心！请在回答中附上下载链接：[📥 点击下载 {safe_name}]({download_url})",
            }

        def bot_open(file, mode="r", *args, **kwargs):
            """智能沙箱 open：所有文件操作强制限定在 BOT 专属产出物目录"""
            return _confined_open(uploads_dir, file, mode, *args, **kwargs)

        stream_cloud_call = next(
            (t for t in tools if isinstance(t, CloudCallTool)), None
        ) or CloudCallTool(
            approval_requester=tool_approval_requester,
            bot_manager=self.manager,
            tim_client=self.tim_client,
        )
        stream_search_tool = (
            next((t for t in tools if isinstance(t, CloudSearchApisTool)), None)
            or CloudSearchApisTool()
        )
        stream_doc_tool = (
            next((t for t in tools if isinstance(t, CloudGetDocTool)), None)
            or CloudGetDocTool()
        )

        _install_sandbox_scope(
            agent,
            {
                "open": bot_open,
                "render_chart": render_chart,
                "save_file": save_file,
                "OUTPUT_DIR": uploads_dir,
                "WORKSPACE_DIR": uploads_dir,
                **build_sandbox_scope(
                    caller_tool=stream_cloud_call,
                    search_tool=stream_search_tool,
                    doc_tool=stream_doc_tool,
                    cloud_client=self.cloud_client,
                    tim_client=self.tim_client,
                ),
            },
        )

        # 初始化线程隔离的实时协同事件收集器
        self._local.active_stream_orchestrations = []
        accumulated_orchestrations = []

        # 4. 构建四层分级记忆与无限上下文 (Hierarchical Infinite Memory)
        from .memory import memory_manager

        effective_context_budget = (
            getattr(bot, "context_budget", None)
            or getattr(llm_cfg, "context_budget", None)
            or DEFAULT_WORKING_MEMORY_CHAR_BUDGET
        )
        effective_context_turns = (
            getattr(bot, "max_context_turns", None)
            or getattr(llm_cfg, "max_context_turns", None)
            or DEFAULT_WORKING_MEMORY_TURNS
        )

        all_history = self.manager.get_history(bot_id, limit=500)
        hierarchical_ctx = memory_manager.build_hierarchical_context(
            bot_id=bot_id,
            bot=bot,
            user_message=user_message,
            all_history=all_history,
            saved_state=saved_state,
            attachments=attachments,
            system_prompt=system_prompt,
            manager=self.manager,
            uploads_dir=uploads_dir,
            context_budget=effective_context_budget,
            max_context_turns=effective_context_turns,
        )
        task_prompt = hierarchical_ctx.task_prompt

        final_answer = ""
        accumulated_thoughts = []
        accumulated_code_blocks = []
        accumulated_execution_logs = []
        accumulated_tool_calls = []

        current_step = 1
        thought_buffer = ""
        in_think_tag = False
        is_in_code = False

        try:
            # 采用 agent.run(task, stream=True) 触发真正的 Token 与事件级全流式输出
            for step in cast(Iterable[Any], agent.run(task_prompt, stream=True)):
                if stop_checker and stop_checker():
                    logger.info("Agent run %s was stopped by user request.", bot_id)
                    return

                # 1. 模型原始 Token 流式增量 (ChatMessageStreamDelta)
                if isinstance(step, ChatMessageStreamDelta):
                    delta = step.content or ""
                    if delta:
                        # 识别 <think> 思考链标签 (DeepSeek-R1 / Qwen 等)
                        if "<think>" in delta and "</think>" in delta:
                            clean = delta.replace("<think>", "").replace("</think>", "")
                            if clean:
                                thought_buffer += clean
                                yield {
                                    "event": "thought_chunk",
                                    "data": {"delta": clean},
                                }
                        elif "<think>" in delta:
                            in_think_tag = True
                            clean = delta.replace("<think>", "")
                            if clean:
                                thought_buffer += clean
                                yield {
                                    "event": "thought_chunk",
                                    "data": {"delta": clean},
                                }
                        elif "</think>" in delta:
                            in_think_tag = False
                            clean = delta.replace("</think>", "")
                            if clean:
                                thought_buffer += clean
                                yield {
                                    "event": "thought_chunk",
                                    "data": {"delta": clean},
                                }
                        elif in_think_tag:
                            thought_buffer += delta
                            yield {"event": "thought_chunk", "data": {"delta": delta}}
                        else:
                            # 监测代码块标记
                            if "<code>" in delta or "```python" in delta:
                                is_in_code = True
                            elif "</code>" in delta or "```" in delta:
                                is_in_code = False

                            # 只有在非代码块时才作为思考流推送，避免代码未解析前出现闪烁
                            if not is_in_code and not delta.strip().startswith("```"):
                                thought_buffer += delta
                                yield {
                                    "event": "thought_chunk",
                                    "data": {"delta": delta},
                                }

                # 2. 思考规划步 (PlanningStep)
                elif isinstance(step, PlanningStep):
                    plan_text = getattr(step, "plan", "") or str(step)
                    if plan_text:
                        plan_md = f"#### 📋 任务规划与路径拆解\n\n{plan_text.strip()}"
                        accumulated_thoughts.append(plan_md)
                        yield {"event": "thought", "data": {"content": plan_md}}

                # 3. 准备执行 Python 脚本 (ToolCall) -> 实时唤醒前端单卡并进入运行中动效
                elif isinstance(step, ToolCall):
                    raw_code = (step.arguments or "").strip()
                    if raw_code.startswith("```python"):
                        raw_code = raw_code[9:]
                    elif raw_code.startswith("```"):
                        raw_code = raw_code[3:]
                    raw_code = raw_code.removesuffix("```")
                    raw_code = raw_code.strip()

                    code_item = {
                        "step": current_step,
                        "code": raw_code,
                        "status": "running",
                    }
                    accumulated_code_blocks.append(code_item)
                    yield {"event": "code_start", "data": code_item}

                # 4. 代码执行返回结果 (ActionOutput)
                elif isinstance(step, ActionOutput):
                    sync_generated_artifacts()
                    if step.is_final_answer:
                        final_answer = str(step.output)
                    else:
                        obs_md = format_execution_observation(step.output, current_step)
                        log_item = {
                            "step": current_step,
                            "output": obs_md,
                            "status": "success",
                        }
                        accumulated_execution_logs.append(log_item)
                        for c in accumulated_code_blocks:
                            if (
                                c.get("step") == current_step
                                and c.get("status") == "running"
                            ):
                                c["status"] = "success"
                        yield {"event": "execution_result", "data": log_item}

                # 5. 代码执行完成步 (ActionStep)
                elif isinstance(step, ActionStep):
                    sync_generated_artifacts()
                    current_step = step.step_number + 1

                    # 补充思维链
                    if step.model_output and not thought_buffer:
                        clean_thought = extract_clean_thought(str(step.model_output))
                        if clean_thought:
                            thought_md = f"#### 💭 步骤 {step.step_number} 推理分析\n\n{clean_thought.strip()}"
                            accumulated_thoughts.append(thought_md)
                            yield {"event": "thought", "data": {"content": thought_md}}

                    # 若沙箱有额外 observations 输出，且尚未向前端派发
                    if step.observations and not any(
                        log["step"] == step.step_number
                        for log in accumulated_execution_logs
                    ):
                        obs_md = format_execution_observation(
                            step.observations, step.step_number
                        )
                        log_item = {
                            "step": step.step_number,
                            "output": obs_md,
                            "status": "success",
                        }
                        accumulated_execution_logs.append(log_item)
                        for c in accumulated_code_blocks:
                            if (
                                c.get("step") == step.step_number
                                and c.get("status") == "running"
                            ):
                                c["status"] = "success"
                        yield {"event": "execution_result", "data": log_item}

                # 6. 最终答案步 (FinalAnswerStep)
                elif isinstance(step, FinalAnswerStep):
                    if step.output:
                        final_answer = str(step.output)

                # 7. 备用文本块
                elif hasattr(step, "content"):
                    delta = getattr(step, "content", "")
                    if delta:
                        final_answer += delta
                elif isinstance(step, str):
                    final_answer = step

                # 检查并派发实时协同事件 (Multi-Agent Orchestration Events)
                orch_queue = getattr(self._local, "active_stream_orchestrations", None)
                while orch_queue:
                    orch_item = orch_queue.pop(0)
                    accumulated_orchestrations.append(orch_item)
                    yield {"event": "orchestration", "data": orch_item}

            # 结算残余协同事件
            orch_queue = getattr(self._local, "active_stream_orchestrations", None)
            while orch_queue:
                orch_item = orch_queue.pop(0)
                accumulated_orchestrations.append(orch_item)
                yield {"event": "orchestration", "data": orch_item}

            # 8. 判断是否触发了最大执行步数限制
            is_max_steps_reached = any(
                isinstance(getattr(st, "error", None), AgentMaxStepsError)
                or "Reached max steps" in str(getattr(st, "error", ""))
                for st in getattr(agent.memory, "steps", [])
            ) or (len(accumulated_code_blocks) >= effective_max_steps)

            # 剥离最终答案中可能残留的 <think> 思维链与系统协议
            if final_answer:
                cleaned_ans, leftover_thought = clean_final_answer(final_answer)
                if leftover_thought:
                    leftover_md = (
                        f"#### 💭 综合校验与归纳\n\n{leftover_thought.strip()}"
                    )
                    accumulated_thoughts.append(leftover_md)
                    yield {"event": "thought", "data": {"content": leftover_md}}
                final_answer = cleaned_ans or "任务已执行完毕。"
            else:
                final_answer = "任务已执行完毕。"

            # 智能抢救：若达到步数上限且最终文本仍然包含未执行的代码块，尝试在沙箱中补执行
            if is_max_steps_reached and "```python" in final_answer:
                code_matches = re.findall(
                    r"```(?:python|py)?\s*\n([\s\S]*?)\n```", final_answer
                )
                for unexecuted_code in code_matches:
                    unexecuted_code = unexecuted_code.strip()
                    if (
                        unexecuted_code
                        and not unexecuted_code.startswith("final_answer")
                        and len(unexecuted_code) > 20
                    ):
                        try:
                            rescue_step = current_step
                            current_step += 1
                            accumulated_code_blocks.append(
                                {
                                    "step": rescue_step,
                                    "code": unexecuted_code,
                                    "status": "running",
                                }
                            )
                            yield {
                                "event": "code_start",
                                "data": {
                                    "step": rescue_step,
                                    "code": unexecuted_code,
                                    "status": "running",
                                },
                            }
                            rescue_res = agent.python_executor(unexecuted_code)
                            rescue_logs = (
                                rescue_res.logs.strip()
                                if rescue_res and rescue_res.logs
                                else ""
                            )
                            obs_md = format_execution_observation(
                                rescue_logs, rescue_step
                            )
                            accumulated_execution_logs.append(
                                {
                                    "step": rescue_step,
                                    "output": obs_md,
                                    "status": "success",
                                }
                            )
                            for c in accumulated_code_blocks:
                                if c.get("step") == rescue_step:
                                    c["status"] = "success"
                            yield {
                                "event": "execution_result",
                                "data": {
                                    "step": rescue_step,
                                    "output": obs_md,
                                    "status": "success",
                                },
                            }
                        except Exception as rex:
                            logger.warning("Rescue execution failed: %s", str(rex))

            # 步数上限友情提示
            if (
                is_max_steps_reached
                and "最大步数" not in final_answer
                and "步数上限" not in final_answer
            ):
                final_answer = (
                    final_answer
                    + f"\n\n> ℹ️ **说明**：该轮任务已达单次执行步数上限（已执行 {len(accumulated_code_blocks)} 步并记录沙箱数据）。相关变量与结果已保存在当前会话环境中，您可以直接回复继续操作或导出数据！"
                )

            # 全流程产出物最终同步归集
            sync_generated_artifacts()

            # 若有产出物文件，检查 final_answer 是否包含直接下载链接；若缺失则自动追加规范下载区
            if tracked_output_files:
                missing_download_links = []
                for f in tracked_output_files:
                    fn = f["name"]
                    sn = f.get("saved_name", fn)
                    if (
                        f"/files/{sn}" not in final_answer
                        and f"/files/{fn}" not in final_answer
                    ):
                        missing_download_links.append(f)

                if missing_download_links:
                    download_md_lines = ["\n\n---\n### 📥 产出物文件直接下载\n"]
                    for mf in missing_download_links:
                        dl_url = (
                            mf.get("download_url")
                            or f"/api/agent/bots/{bot_id}/files/{mf['saved_name']}?download=true"
                        )
                        download_md_lines.append(
                            f"- [📥 **{mf['name']}** ({mf['size_formatted']})]({dl_url})\n"
                        )
                    final_answer = final_answer + "".join(download_md_lines)

            # 触发流式回答生命周期：通知前端折叠过程、聚焦正文，开启打字机效果
            yield {"event": "answer_start", "data": {"timestamp": time.time()}}

            # 自适应平滑打字机流式推送 (Token-like Natural Typing Stream)
            # 根据内容长度自适应块大小与延时，避免长文本渲染产生过长的人为等待
            total_len = len(final_answer)
            if total_len > 2000:
                chunk_size = 24
                sleep_time = 0.003
            elif total_len > 800:
                chunk_size = 12
                sleep_time = 0.006
            else:
                chunk_size = 6
                sleep_time = 0.010

            for i in range(0, total_len, chunk_size):
                chunk_piece = final_answer[i : i + chunk_size]
                yield {"event": "chunk", "data": {"delta": chunk_piece}}
                time.sleep(sleep_time)

            # 发送最终整理好的完整回答
            yield {"event": "message", "data": {"content": final_answer}}

            # 存入历史（完整持久化结构化思维链、Python 代码、执行日志与工具调用）
            if thought_buffer and not accumulated_thoughts:
                accumulated_thoughts.append(thought_buffer.strip())

            # 确保保存历史记录时，所有已执行的代码块状态均更新为 success (除非此前为 error)
            for c in accumulated_code_blocks:
                if c.get("status") == "running":
                    c["status"] = "success"

            full_thought_str = "\n\n---\n\n".join(
                [t for t in accumulated_thoughts if t.strip()]
            ).strip()

            # 统计本次执行的性能与消耗指标
            stream_elapsed = round(time.time() - stream_start_time, 2)
            input_tokens = 0
            output_tokens = 0
            if hasattr(agent, "monitor") and agent.monitor:
                try:
                    usage = agent.monitor.get_total_token_counts()
                    raw_in = getattr(usage, "input_tokens", None)
                    raw_out = getattr(usage, "output_tokens", None)
                    if isinstance(raw_in, (int, float)):
                        input_tokens = int(raw_in)
                    if isinstance(raw_out, (int, float)):
                        output_tokens = int(raw_out)
                except Exception as usage_error:
                    logger.debug("Failed to collect token usage: %s", usage_error)

            if input_tokens == 0 and output_tokens == 0:
                # 兼容未回传 token usage 的提供商的保底估算
                input_tokens = max(10, len(task_prompt) // 2)
                output_tokens = max(10, (len(final_answer) + len(thought_buffer)) // 2)

            total_tokens = input_tokens + output_tokens
            metrics = {
                "elapsed_seconds": stream_elapsed,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "code_executions": len(accumulated_code_blocks),
                "model": llm_cfg.model_name,
            }
            # 检查若已被用户中止，跳过历史持久化与指标记录
            if stop_checker and stop_checker():
                logger.info("Agent run %s was stopped, skipping history save.", bot_id)
                return

            # 记录到智能体累计指标库
            self.manager.record_run_stats(bot_id, metrics)

            self.manager.append_history(
                bot_id,
                "assistant",
                final_answer,
                metadata={
                    "thought": full_thought_str,
                    "code_blocks": accumulated_code_blocks,
                    "execution_logs": accumulated_execution_logs,
                    "tool_calls": accumulated_tool_calls,
                    "orchestrations": accumulated_orchestrations,
                    "code_count": len(accumulated_code_blocks),
                    "model": llm_cfg.model_name,
                    "metrics": metrics,
                    "attachments": tracked_output_files,
                    "artifacts": tracked_output_files,
                },
            )

            # 保存沙箱环境供后续跨轮交互直接复用
            if hasattr(agent, "python_executor") and hasattr(
                agent.python_executor, "state"
            ):
                persistent_vars = {}
                executor = cast(Any, agent.python_executor)
                for k, v in executor.state.items():
                    if k.startswith("_") or k in (
                        "open",
                        "render_chart",
                        "save_file",
                        "OUTPUT_DIR",
                        "WORKSPACE_DIR",
                        "cloud_client",
                        "tim_client",
                        "get_usersig",
                        "final_answer",
                    ):
                        continue
                    if callable(v) and not hasattr(v, "__code__"):
                        continue
                    persistent_vars[k] = v
                self._remember_sandbox(self._sandbox_key(bot_id), persistent_vars)

            # 触发四层分级记忆演化归档与增量滚动摘要更新
            try:
                from .memory import memory_manager

                memory_manager.on_turn_completed(
                    bot_id=bot_id,
                    bot_dir=self.manager._runtime_dir(bot_id),
                    all_history=self.manager.get_history(bot_id, limit=500),
                    saved_state=self._get_sandbox(self._sandbox_key(bot_id)),
                    max_context_turns=effective_context_turns,
                )
            except Exception as mex:
                logger.warning("Memory manager on_turn_completed error: %s", str(mex))

            yield {
                "event": "done",
                "data": {
                    "status": "max_steps_reached"
                    if is_max_steps_reached
                    else "success",
                    "code_blocks": len(accumulated_code_blocks),
                    "orchestrations": accumulated_orchestrations,
                    "metrics": metrics,
                    "timestamp": time.time(),
                    "attachments": tracked_output_files,
                    "files": tracked_output_files,
                },
            }

        except Exception as e:
            err_text = f"智能体执行遇到异常: {e!s}"
            logger.exception("Agent run error: %s", err_text)
            yield {"event": "error", "data": {"error": err_text}}
            self.manager.append_history(bot_id, "assistant", f"[异常] {err_text}")
        finally:
            self._local.active_stream_orchestrations = None
            if execution_controller:
                execution_controller(None)
            executor.close()


agent_engine = AgentEngine()
