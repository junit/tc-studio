"""Run a real two-process acceptance probe for TC Studio's SQLite task backend.

The probe starts independent Python interpreters against one SQLite file and
one result directory.  It intentionally uses dry-run tasks, so it exercises
the persistence, lease, control, event, and result-index boundaries without
calling Tencent Cloud.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHILD_FLAG = "--child"
TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED", "UNKNOWN"}
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _json_line(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True)


def _child_environment(
    *,
    database: Path,
    storage: Path,
    results: Path,
    history: Path,
) -> dict[str, str]:
    environment = dict(os.environ)
    current_python_path = environment.get("PYTHONPATH", "")
    project_path = str(PROJECT_ROOT)
    environment["PYTHONPATH"] = (
        project_path
        if not current_python_path
        else project_path + os.pathsep + current_python_path
    )
    environment["TC_STUDIO_DATABASE_URL"] = f"sqlite:///{database}"
    environment["TC_STUDIO_SQLITE_PATH"] = str(database)
    environment["TC_STUDIO_CONFIG_PATH"] = str(database.with_name("config.json"))
    environment["TC_STUDIO_STORAGE_DIR"] = str(storage)
    environment["TC_STUDIO_RESULTS_DIR"] = str(results)
    environment["TC_STUDIO_HISTORY_PATH"] = str(history)
    return environment


def _child_main(arguments: argparse.Namespace) -> int:
    import core.task_runner as task_runner_module
    from core.persistence import SqliteTaskStore
    from core.task_runner import (
        TaskRunner,
        batch_request_fingerprint,
        task_result_consistency,
    )

    if arguments.lease_seconds is not None:
        task_runner_module.BATCH_TASK_LEASE_SECONDS = max(
            1,
            int(arguments.lease_seconds),
        )

    if arguments.operation == "submit":
        items = [{"To_Account": "acceptance-user"}]
        fingerprint = batch_request_fingerprint(
            "c2c_msg_withdraw",
            items,
            0,
            True,
            False,
        )
        runner = TaskRunner()
        task = runner.create_task(
            "c2c_msg_withdraw",
            items,
            delay_sec=0,
            dry_run=True,
            owner_id="acceptance-owner",
            issuer="acceptance-issuer",
            tenant_id="acceptance-tenant",
            idempotency_key=arguments.idempotency_key,
            request_fingerprint=fingerprint,
        )
        started = runner.start_task(task.task_id, recover_reserved=True)
        deadline = time.monotonic() + arguments.timeout
        record = runner.get_task_record(task.task_id)
        while (
            record is not None
            and str(record.get("status")) not in TERMINAL_STATUSES
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
            record = runner.get_task_record(task.task_id)
        if record is None or str(record.get("status")) not in TERMINAL_STATUSES:
            raise RuntimeError(f"任务未在超时时间内结束: {task.task_id}")
        _json_line(
            {
                "operation": "submit",
                "task_id": task.task_id,
                "idempotent_replay": bool(task.idempotent_replay),
                "started": bool(started),
                "status": record.get("status"),
                "worker_id": runner.worker_id,
                "result_consistency": task_result_consistency(task.task_id),
            }
        )
        return 0

    if arguments.operation == "create":
        runner = TaskRunner()
        items = [
            {"To_Account": f"acceptance-user-{index}"}
            for index in range(arguments.items)
        ]
        task = runner.create_task(
            "c2c_msg_withdraw",
            items,
            delay_sec=arguments.delay,
            dry_run=True,
            owner_id="acceptance-owner",
            issuer="acceptance-issuer",
            tenant_id="acceptance-tenant",
        )
        _json_line(
            {
                "operation": "create",
                "task_id": task.task_id,
                "status": runner.get_task_record(task.task_id)["status"],
            }
        )
        return 0

    if arguments.operation == "worker":
        runner = TaskRunner()
        task = runner.get_task(arguments.task_id)
        if task is None:
            raise RuntimeError(f"找不到任务: {arguments.task_id}")
        started = runner.start_task(arguments.task_id, recover_reserved=True)
        deadline = time.monotonic() + arguments.timeout
        record = runner.get_task_record(arguments.task_id)
        while (
            record is not None
            and str(record.get("status")) not in TERMINAL_STATUSES
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
            record = runner.get_task_record(arguments.task_id)
        if record is None or str(record.get("status")) not in TERMINAL_STATUSES:
            raise RuntimeError(f"Worker 未在超时时间内结束: {arguments.task_id}")
        events = runner.list_task_events(arguments.task_id)
        _json_line(
            {
                "operation": "worker",
                "task_id": arguments.task_id,
                "started": bool(started),
                "status": record.get("status"),
                "event_types": [event.get("type") for _sequence, event in events],
            }
        )
        return 0

    if arguments.operation == "control":
        runner = TaskRunner()
        record = runner.control_task(arguments.task_id, arguments.action)
        if record is None:
            raise RuntimeError(f"控制目标不存在: {arguments.task_id}")
        _json_line(
            {
                "operation": "control",
                "task_id": arguments.task_id,
                "action": arguments.action,
                "status": record.get("status"),
            }
        )
        return 0

    if arguments.operation == "observe":
        runner = TaskRunner()
        record = runner.get_task_record(arguments.task_id)
        if record is None:
            raise RuntimeError(f"观察目标不存在: {arguments.task_id}")
        _json_line(
            {
                "operation": "observe",
                "task_id": arguments.task_id,
                "status": record.get("status"),
                "worker_id": record.get("worker_id"),
            }
        )
        return 0

    if arguments.operation == "observe-until":
        runner = TaskRunner()
        deadline = time.monotonic() + arguments.timeout
        record = None
        while time.monotonic() < deadline:
            record = runner.get_task_record(arguments.task_id)
            if record is not None and str(record.get("status")) == arguments.expected_status:
                _json_line(
                    {
                        "operation": "observe-until",
                        "task_id": arguments.task_id,
                        "status": record.get("status"),
                        "worker_id": record.get("worker_id"),
                        "current_index": record.get("current_index"),
                        "success_count": record.get("success_count"),
                        "failed_count": record.get("failed_count"),
                    }
                )
                return 0
            time.sleep(0.1)
        raise RuntimeError(
            f"任务未进入预期状态: task_id={arguments.task_id}, "
            f"expected={arguments.expected_status}, record={record}"
        )

    if arguments.operation == "results-check":
        _json_line(
            {
                "operation": "results-check",
                "task_id": arguments.task_id,
                "result_consistency": task_result_consistency(arguments.task_id),
            }
        )
        return 0

    if arguments.operation == "export-check":
        from fastapi import HTTPException

        from app import export_csv

        try:
            response = export_csv(arguments.task_id)
        except HTTPException as exc:
            _json_line(
                {
                    "operation": "export-check",
                    "task_id": arguments.task_id,
                    "status_code": exc.status_code,
                    "detail": str(exc.detail),
                }
            )
        else:
            _json_line(
                {
                    "operation": "export-check",
                    "task_id": arguments.task_id,
                    "status_code": 200,
                    "response_type": type(response).__name__,
                }
            )
        return 0

    if arguments.operation == "persistence-write-check":
        runner = TaskRunner()
        try:
            task = runner.create_task(
                "c2c_msg_withdraw",
                [{"To_Account": "readonly-check"}],
                delay_sec=0,
                dry_run=True,
                owner_id="acceptance-owner",
                issuer="acceptance-issuer",
                tenant_id="acceptance-tenant",
            )
        except Exception as exc:
            _json_line(
                {
                    "operation": "persistence-write-check",
                    "accepted": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:256],
                }
            )
            return 0
        _json_line(
            {
                "operation": "persistence-write-check",
                "accepted": True,
                "task_id": task.task_id,
            }
        )
        return 0

    if arguments.operation == "create-stale":
        store = SqliteTaskStore()
        now = time.time()
        created = store.create_batch_task(
            {
                "task_id": arguments.task_id,
                "issuer": "acceptance-issuer",
                "tenant_id": "acceptance-tenant",
                "owner_subject": "acceptance-owner",
                "api_id": "c2c_msg_withdraw",
                "status": "RUNNING",
                "total": 1,
                "items_json": '[{"To_Account":"acceptance-stale"}]',
                "worker_id": "dead-worker",
                "lease_until": now - 1,
                "last_heartbeat": now - 61,
            }
        )
        if not created:
            raise RuntimeError(f"无法创建过期租约任务: {arguments.task_id}")
        _json_line(
            {
                "operation": "create-stale",
                "task_id": arguments.task_id,
                "status": "RUNNING",
                "lease_until": now - 1,
            }
        )
        return 0

    if arguments.operation == "stale-finalize":
        store = SqliteTaskStore()
        accepted = store.set_task_terminal(
            arguments.task_id,
            status="COMPLETED",
            worker_id=arguments.worker_id or "dead-worker",
            current_index=1,
            success_count=1,
            failed_count=0,
        )
        _json_line(
            {
                "operation": "stale-finalize",
                "task_id": arguments.task_id,
                "accepted": bool(accepted),
            }
        )
        return 0

    raise ValueError(f"未知子进程操作: {arguments.operation}")


def _start_child(
    *,
    operation: str,
    environment: dict[str, str],
    extra: list[str] | None = None,
) -> subprocess.Popen[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        CHILD_FLAG,
        operation,
    ]
    if extra:
        command.extend(extra)
    return subprocess.Popen(
        command,
        cwd=str(PROJECT_ROOT),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _collect_child(
    process: subprocess.Popen[str],
    *,
    timeout: float,
) -> dict[str, Any]:
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise RuntimeError(
            f"子进程超时: stdout={stdout[-2000:]!r}, stderr={stderr[-2000:]!r}"
        )
    if process.returncode != 0:
        raise RuntimeError(
            f"子进程失败({process.returncode}): "
            f"stdout={stdout[-2000:]!r}, stderr={stderr[-4000:]!r}"
        )
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"子进程没有 JSON 输出: stderr={stderr[-4000:]!r}")
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"子进程输出不是 JSON: stdout={stdout[-4000:]!r}, stderr={stderr[-4000:]!r}"
        ) from exc


def _run_child(
    *,
    operation: str,
    environment: dict[str, str],
    timeout: float,
    extra: list[str] | None = None,
) -> dict[str, Any]:
    return _collect_child(
        _start_child(operation=operation, environment=environment, extra=extra),
        timeout=timeout,
    )


def _wait_for_status(
    store: Any,
    task_id: str,
    expected: set[str],
    *,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    record = None
    while time.monotonic() < deadline:
        record = store.get_batch_task(task_id)
        if record is not None and str(record.get("status")) in expected:
            return record
        time.sleep(0.05)
    raise RuntimeError(
        f"任务未进入预期状态: task_id={task_id}, expected={sorted(expected)}, "
        f"record={record}"
    )


def _wait_for_event(
    store: Any,
    task_id: str,
    event_type: str,
    *,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = store.list_task_events(task_id)
        if any(event.get("type") == event_type for _sequence, event in events):
            return
        time.sleep(0.05)
    raise RuntimeError(
        f"任务未出现预期事件: task_id={task_id}, event_type={event_type}"
    )


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _run_hard_kill_probe(
    *,
    work_dir: Path,
    environment: dict[str, str],
    store: Any,
    timeout: float,
    lease_seconds: int,
) -> dict[str, Any]:
    task_id = _run_child(
        operation="create",
        environment=environment,
        timeout=timeout,
        extra=["--items", "80", "--delay", "0.1"],
    )["task_id"]
    worker = _start_child(
        operation="worker",
        environment=environment,
        extra=[
            "--task-id",
            task_id,
            "--timeout",
            str(timeout),
            "--lease-seconds",
            str(lease_seconds),
        ],
    )
    _wait_for_status(store, task_id, {"RUNNING"}, timeout=timeout)
    _wait_for_event(store, task_id, "progress", timeout=timeout)
    running = _run_child(
        operation="observe",
        environment=environment,
        timeout=timeout,
        extra=["--task-id", task_id],
    )
    _assert(running["status"] == "RUNNING", "强杀前任务没有处于 RUNNING")
    worker.kill()
    worker_returncode = worker.wait(timeout=10)
    _assert(
        worker_returncode < 0,
        f"Worker 没有被 SIGKILL 终止: returncode={worker_returncode}",
    )

    observer = _start_child(
        operation="observe-until",
        environment=environment,
        extra=[
            "--task-id",
            task_id,
            "--expected-status",
            "UNKNOWN",
            "--timeout",
            str(max(timeout, lease_seconds + 10)),
        ],
    )
    unknown = _collect_child(
        observer,
        timeout=max(timeout, lease_seconds + 15),
    )
    _assert(unknown["status"] == "UNKNOWN", "真实 Worker 被强杀后没有进入 UNKNOWN")
    result_check = _run_child(
        operation="results-check",
        environment=environment,
        timeout=timeout,
        extra=["--task-id", task_id],
    )
    _assert(
        result_check["result_consistency"]["consistent"],
        "真实 Worker 强杀后的结果文件和 SQLite 索引不一致",
    )
    stale_finalize = _run_child(
        operation="stale-finalize",
        environment=environment,
        timeout=timeout,
        extra=["--task-id", task_id, "--worker-id", running["worker_id"] or ""],
    )
    _assert(
        stale_finalize["accepted"] is False,
        "被强杀 Worker 的旧 worker_id 仍然可以覆盖 UNKNOWN",
    )
    final = _run_child(
        operation="observe",
        environment=environment,
        timeout=timeout,
        extra=["--task-id", task_id],
    )
    _assert(final["status"] == "UNKNOWN", "旧 Worker 写入后任务不再是 UNKNOWN")
    return {
        "task_id": task_id,
        "worker_id": running["worker_id"],
        "lease_seconds": lease_seconds,
        "result_consistency": result_check["result_consistency"],
    }


def _run_file_fault_probe(
    *,
    work_dir: Path,
    database: Path,
    storage: Path,
    results: Path,
    history: Path,
    environment: dict[str, str],
    timeout: float,
    http_export: bool,
) -> dict[str, Any]:
    from core.task_runner import task_results_path

    task_id = _run_child(
        operation="submit",
        environment=environment,
        timeout=timeout,
        extra=[
            "--idempotency-key",
            "sqlite-acceptance-file-fault-key",
            "--timeout",
            str(timeout),
        ],
    )["task_id"]
    result_path = Path(task_results_path(task_id))
    original = result_path.read_bytes()
    baseline = _run_child(
        operation="results-check",
        environment=environment,
        timeout=timeout,
        extra=["--task-id", task_id],
    )
    _assert(
        baseline["result_consistency"]["consistent"],
        "文件故障验收的基线结果不一致",
    )

    result_path.write_bytes(original[:-1])
    truncated = _run_child(
        operation="results-check",
        environment=environment,
        timeout=timeout,
        extra=["--task-id", task_id],
    )
    _assert(
        truncated["result_consistency"]["consistent"] is False,
        "结果文件截断后仍被判定为一致",
    )
    if http_export:
        truncated_export = _run_child(
            operation="export-check",
            environment=environment,
            timeout=timeout,
            extra=["--task-id", task_id],
        )
        _assert(
            truncated_export["status_code"] == 409,
            "结果文件截断后导出接口没有返回 409",
        )
    result_path.write_bytes(original)

    tampered = original.replace(
        b"acceptance-user",
        b"x" * len(b"acceptance-user"),
        1,
    )
    _assert(tampered != original, "文件篡改验收没有生成有效变更")
    result_path.write_bytes(tampered)
    tampered_check = _run_child(
        operation="results-check",
        environment=environment,
        timeout=timeout,
        extra=["--task-id", task_id],
    )
    _assert(
        tampered_check["result_consistency"]["consistent"] is False,
        "同长度结果文件篡改没有被 SHA-256 索引校验发现",
    )
    result_path.write_bytes(original)

    result_path.unlink()
    missing = _run_child(
        operation="results-check",
        environment=environment,
        timeout=timeout,
        extra=["--task-id", task_id],
    )
    _assert(
        missing["result_consistency"]["consistent"] is False,
        "结果文件删除后仍被判定为一致",
    )
    if http_export:
        missing_export = _run_child(
            operation="export-check",
            environment=environment,
            timeout=timeout,
            extra=["--task-id", task_id],
        )
        _assert(
            missing_export["status_code"] == 409,
            "结果文件删除后导出接口没有返回 409",
        )
    result_path.write_bytes(original)

    result_path.chmod(0)
    permission_check = _run_child(
        operation="results-check",
        environment=environment,
        timeout=timeout,
        extra=["--task-id", task_id],
    )
    _assert(
        permission_check["result_consistency"]["consistent"] is False,
        "结果文件不可读时没有进入失败闭环",
    )
    result_path.chmod(0o600)

    from core.persistence import open_database

    readonly_database = work_dir / "readonly.sqlite3"
    source = open_database(str(database))
    try:
        target = sqlite3.connect(str(readonly_database))
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
    finally:
        source.close()
    readonly_database.chmod(0o400)
    readonly_environment = _child_environment(
        database=readonly_database,
        storage=storage,
        results=results,
        history=history,
    )
    readonly_write = _run_child(
        operation="persistence-write-check",
        environment=readonly_environment,
        timeout=timeout,
    )
    _assert(
        readonly_write["accepted"] is False,
        "SQLite 只读时写任务仍被接受",
    )
    _assert(
        readonly_write["error_type"] in {"OperationalError", "DatabaseError", "OSError"},
        f"SQLite 只读错误没有返回可识别类型: {readonly_write}",
    )

    return {
        "task_id": task_id,
        "checks": {
            "truncated_result_rejected": True,
            "same_length_tamper_rejected": True,
            "missing_result_rejected": True,
            "unreadable_result_rejected": True,
            "readonly_database_write_rejected": True,
        },
        "http_export_checks": (
            {
                "truncated_export_409": True,
                "missing_export_409": True,
            }
            if http_export
            else {
                "skipped": True,
                "reason": "Linux 标准库验收容器未安装 FastAPI/Agent 运行依赖",
            }
        ),
        "readonly_database": str(readonly_database),
    }


def _run_probe(
    work_dir: Path,
    timeout: float,
    *,
    hard_kill: bool = False,
    file_faults: bool = False,
    http_export: bool = True,
    hard_kill_lease_seconds: int = 5,
) -> dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    database = work_dir / "tc_studio.sqlite3"
    storage = work_dir / "storage"
    results = work_dir / "batch_results"
    history = storage / "history.json"
    environment = _child_environment(
        database=database,
        storage=storage,
        results=results,
        history=history,
    )
    os.environ.update(
        {
            "TC_STUDIO_DATABASE_URL": f"sqlite:///{database}",
            "TC_STUDIO_SQLITE_PATH": str(database),
            "TC_STUDIO_STORAGE_DIR": str(storage),
            "TC_STUDIO_RESULTS_DIR": str(results),
            "TC_STUDIO_HISTORY_PATH": str(history),
        }
    )

    key = "sqlite-acceptance-concurrent-key"
    first = _start_child(
        operation="submit",
        environment=environment,
        extra=["--idempotency-key", key, "--timeout", str(timeout)],
    )
    second = _start_child(
        operation="submit",
        environment=environment,
        extra=["--idempotency-key", key, "--timeout", str(timeout)],
    )
    first_result = _collect_child(first, timeout=timeout)
    second_result = _collect_child(second, timeout=timeout)
    _assert(
        first_result["task_id"] == second_result["task_id"],
        "并发幂等请求产生了不同 task_id",
    )
    _assert(
        {bool(first_result["idempotent_replay"]), bool(second_result["idempotent_replay"])}
        == {False, True},
        "并发幂等请求没有形成一个新建、一个重放",
    )
    _assert(
        first_result["status"] == second_result["status"] == "COMPLETED",
        "并发 dry-run 任务没有稳定完成",
    )
    _assert(
        first_result["result_consistency"]["consistent"]
        and second_result["result_consistency"]["consistent"],
        "并发任务结果文件和 SQLite 索引不一致",
    )

    from core.persistence import SqliteTaskStore
    from core.task_runner import task_result_consistency

    store = SqliteTaskStore(str(database))
    task_id = _run_child(
        operation="create",
        environment=environment,
        timeout=timeout,
        extra=["--items", "80", "--delay", "0.1"],
    )["task_id"]
    worker = _start_child(
        operation="worker",
        environment=environment,
        extra=["--task-id", task_id, "--timeout", str(timeout)],
    )
    _wait_for_status(store, task_id, {"RUNNING"}, timeout=timeout)
    _wait_for_event(store, task_id, "start", timeout=timeout)

    pause_result = _run_child(
        operation="control",
        environment=environment,
        timeout=timeout,
        extra=["--task-id", task_id, "--action", "pause"],
    )
    _assert(pause_result["status"] == "PAUSED", "跨进程 pause 没有写入 PAUSED")
    _wait_for_status(store, task_id, {"PAUSED"}, timeout=timeout)

    resume_result = _run_child(
        operation="control",
        environment=environment,
        timeout=timeout,
        extra=["--task-id", task_id, "--action", "resume"],
    )
    _assert(resume_result["status"] == "RUNNING", "跨进程 resume 没有写回 RUNNING")

    stop_result = _run_child(
        operation="control",
        environment=environment,
        timeout=timeout,
        extra=["--task-id", task_id, "--action", "stop"],
    )
    _assert(stop_result["status"] == "CANCELLED", "跨进程 stop 没有写入 CANCELLED")
    worker_result = _collect_child(worker, timeout=timeout)
    _assert(worker_result["status"] == "CANCELLED", "Worker 覆盖了远程 CANCELLED")
    _assert(
        task_result_consistency(task_id)["consistent"],
        "跨进程控制任务结果索引不一致",
    )

    stale_id = "sqlite-acceptance-stale-task"
    _run_child(
        operation="create-stale",
        environment=environment,
        timeout=timeout,
        extra=["--task-id", stale_id],
    )
    stale_observation = _run_child(
        operation="observe",
        environment=environment,
        timeout=timeout,
        extra=["--task-id", stale_id],
    )
    _assert(
        stale_observation["status"] == "UNKNOWN",
        "独立进程读取过期租约后没有进入 UNKNOWN",
    )
    stale_finalize = _run_child(
        operation="stale-finalize",
        environment=environment,
        timeout=timeout,
        extra=["--task-id", stale_id],
    )
    _assert(
        stale_finalize["accepted"] is False,
        "旧 Worker 仍然可以覆盖 UNKNOWN 任务",
    )
    final_stale = store.get_batch_task(stale_id)
    _assert(
        final_stale is not None and final_stale["status"] == "UNKNOWN",
        "过期租约任务最终状态不是 UNKNOWN",
    )

    result: dict[str, Any] = {
        "database": str(database),
        "work_dir": str(work_dir),
        "checks": {
            "concurrent_idempotency": True,
            "cross_process_pause_resume_stop": True,
            "stale_lease_quarantine": True,
            "stale_worker_terminal_write_rejected": True,
        },
        "concurrent_task_id": first_result["task_id"],
        "controlled_task_id": task_id,
        "stale_task_id": stale_id,
        "controlled_worker_events": worker_result["event_types"],
    }
    if hard_kill:
        result["hard_kill"] = _run_hard_kill_probe(
            work_dir=work_dir,
            environment=environment,
            store=store,
            timeout=timeout,
            lease_seconds=hard_kill_lease_seconds,
        )
        result["checks"]["real_worker_sigkill_unknown"] = True
        result["checks"]["sigkilled_worker_cannot_finalize"] = True
    if file_faults:
        result["file_faults"] = _run_file_fault_probe(
            work_dir=work_dir,
            database=database,
            storage=storage,
            results=results,
            history=history,
            environment=environment,
            timeout=timeout,
            http_export=http_export,
        )
        result["checks"].update(result["file_faults"]["checks"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("operation", nargs="?")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--task-id")
    parser.add_argument("--action", choices=("pause", "resume", "stop"))
    parser.add_argument("--items", type=int, default=1)
    parser.add_argument("--delay", type=float, default=0.05)
    parser.add_argument("--expected-status")
    parser.add_argument("--worker-id")
    parser.add_argument("--lease-seconds", type=int, default=None)
    parser.add_argument("--hard-kill", action="store_true")
    parser.add_argument("--file-faults", action="store_true")
    parser.add_argument(
        "--skip-http-export",
        action="store_true",
        help="只运行 SQLite/文件层故障验收，跳过需要完整 FastAPI 依赖的导出路由探针",
    )
    parser.add_argument("--hard-kill-lease-seconds", type=int, default=5)
    args = parser.parse_args()

    if args.child:
        if not args.operation:
            raise SystemExit("--child 必须指定 operation")
        return _child_main(args)

    if args.timeout <= 0:
        raise SystemExit("--timeout 必须大于 0")
    if args.hard_kill_lease_seconds < 1:
        raise SystemExit("--hard-kill-lease-seconds 必须大于 0")
    if args.work_dir is not None:
        result = _run_probe(
            args.work_dir.expanduser().resolve(),
            args.timeout,
            hard_kill=args.hard_kill,
            file_faults=args.file_faults,
            http_export=not args.skip_http_export,
            hard_kill_lease_seconds=args.hard_kill_lease_seconds,
        )
        _json_line(result)
        return 0

    with tempfile.TemporaryDirectory(prefix="tim-sqlite-multiprocess-") as path:
        result = _run_probe(
            Path(path),
            args.timeout,
            hard_kill=args.hard_kill,
            file_faults=args.file_faults,
            http_export=not args.skip_http_export,
            hard_kill_lease_seconds=args.hard_kill_lease_seconds,
        )
        _json_line(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
