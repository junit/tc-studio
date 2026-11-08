"""Run an API-level two-process acceptance probe against one SQLite database.

Two independent Uvicorn processes expose the same FastAPI application and
share one local storage directory.  The probe submits the same batch request
concurrently through both HTTP listeners, then verifies cross-process status
visibility and completion.  It uses local auth and dry-run work only.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _request(
    port: int,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    encoded = None
    request_headers = {"Accept": "application/json"}
    if payload is not None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    request = Request(
        f"http://127.0.0.1:{port}{path}",
        data=encoded,
        headers=request_headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=5) as response:
            body = response.read().decode("utf-8")
            return int(response.status), json.loads(body) if body else {}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return int(exc.code), parsed


def _wait_until_ready(port: int, process: subprocess.Popen[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(
                f"Uvicorn 提前退出: returncode={process.returncode}, "
                f"stdout={stdout[-2000:]!r}, stderr={stderr[-4000:]!r}"
            )
        try:
            status, payload = _request(port, "/api/services")
            if status == 200 and payload.get("status") == "success":
                return
            last_error = f"status={status}, payload={payload}"
        except (OSError, URLError, TimeoutError) as exc:
            last_error = repr(exc)
        time.sleep(0.1)
    raise RuntimeError(f"API 进程未在超时时间内就绪: port={port}, last={last_error}")


def _start_server(port: int, environment: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(PROJECT_ROOT),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _submit(port: int, idempotency_key: str) -> tuple[int, dict[str, Any]]:
    return _request(
        port,
        "/api/batch/create",
        method="POST",
        headers={"Idempotency-Key": idempotency_key},
        payload={
            "api_id": "c2c_msg_withdraw",
            "items": [{"To_Account": "http-acceptance-user"}],
            "delay_sec": 1,
            "dry_run": True,
        },
    )


def _run_probe(work_dir: Path, timeout: float) -> dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    storage = work_dir / "storage"
    results = storage / "batch_results"
    data = work_dir / "data"
    database = work_dir / "tc_studio.sqlite3"
    config = work_dir / "config.json"
    history = storage / "history.json"
    ports = [_free_port(), _free_port()]
    while ports[1] == ports[0]:
        ports[1] = _free_port()

    environment = dict(os.environ)
    project_path = str(PROJECT_ROOT)
    current_python_path = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = (
        project_path
        if not current_python_path
        else project_path + os.pathsep + current_python_path
    )
    environment.update(
        {
            "APP_ENV": "development",
            "AUTH_ENABLED": "false",
            "TC_STUDIO_DATABASE_URL": f"sqlite:///{database}",
            "TC_STUDIO_SQLITE_PATH": str(database),
            "TC_STUDIO_CONFIG_PATH": str(config),
            "TC_STUDIO_STORAGE_DIR": str(storage),
            "TC_STUDIO_RESULTS_DIR": str(results),
            "TC_STUDIO_HISTORY_PATH": str(history),
            "TC_STUDIO_DATA_DIR": str(data),
        }
    )

    processes: list[subprocess.Popen[str]] = []
    try:
        processes = [_start_server(port, environment) for port in ports]
        for port, process in zip(ports, processes):
            _wait_until_ready(port, process, timeout)

        key = "http-sqlite-acceptance-key"
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(_submit, port, key) for port in ports]
            submissions = [future.result() for future in futures]

        if any(status != 200 for status, _payload in submissions):
            raise RuntimeError(f"并发 HTTP 提交失败: {submissions!r}")
        task_ids = {
            str(payload.get("task_id"))
            for _status, payload in submissions
            if payload.get("task_id")
        }
        if len(task_ids) != 1:
            raise AssertionError(f"HTTP 幂等请求生成了多个任务: {submissions!r}")
        replay_flags = {
            bool(payload.get("idempotent_replay"))
            for _status, payload in submissions
        }
        if replay_flags != {False, True}:
            raise AssertionError(f"HTTP 幂等重放标记不正确: {submissions!r}")
        task_id = next(iter(task_ids))

        deadline = time.monotonic() + timeout
        status_payloads: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            observations = [_request(port, f"/api/batch/{task_id}") for port in ports]
            if all(status == 200 for status, _payload in observations):
                status_payloads = [
                    payload.get("data") or {} for _status, payload in observations
                ]
                if all(item.get("status") == "COMPLETED" for item in status_payloads):
                    break
            time.sleep(0.1)
        else:
            raise RuntimeError(
                f"两个 HTTP 实例未观察到 COMPLETED: {status_payloads!r}"
            )

        return {
            "database": str(database),
            "work_dir": str(work_dir),
            "ports": ports,
            "task_id": task_id,
            "submissions": [
                {
                    "status": status,
                    "task_id": payload.get("task_id"),
                    "idempotent_replay": payload.get("idempotent_replay"),
                }
                for status, payload in submissions
            ],
            "observations": status_payloads,
            "checks": {
                "two_http_processes_ready": True,
                "concurrent_http_idempotency": True,
                "cross_process_http_status_visibility": True,
                "cross_process_http_completion": True,
            },
        }
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if args.timeout <= 0:
        raise SystemExit("--timeout 必须大于 0")
    if args.work_dir is not None:
        result = _run_probe(args.work_dir.expanduser().resolve(), args.timeout)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    with tempfile.TemporaryDirectory(prefix="tim-http-multiprocess-") as path:
        result = _run_probe(Path(path), args.timeout)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
