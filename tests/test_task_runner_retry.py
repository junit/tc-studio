"""批处理频控重试的行为契约测试。"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import core.task_runner as task_runner_module
from core.task_runner import TaskRunner


class _FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def invoke_api(self, api_meta, payload):
        self.calls.append((api_meta, payload))
        return next(self.responses)


def test_rate_limit_retry_90001_uses_bounded_exponential_backoff(monkeypatch):
    client = _FakeClient([
        {"ActionStatus": "FAIL", "ErrorCode": 90001, "ErrorInfo": "频率超限"},
        {"ActionStatus": "FAIL", "ErrorCode": "90001", "ErrorInfo": "频率超限"},
        {"ActionStatus": "OK", "ErrorCode": 0},
    ])
    sleeps = []
    monkeypatch.setattr(task_runner_module.random, "uniform", lambda _low, _high: 0)
    monkeypatch.setattr(task_runner_module.time, "sleep", sleeps.append)

    response = TaskRunner._invoke_with_rate_limit_retry(client, object(), {"id": 1})

    assert response["ActionStatus"] == "OK"
    assert len(client.calls) == 3
    assert sleeps == [0.2, 0.4]


def test_rate_limit_retry_429_stops_after_three_retries(monkeypatch):
    client = _FakeClient([
        {"ActionStatus": "FAIL", "ErrorCode": 429, "ErrorInfo": "HTTP 429"}
        for _ in range(4)
    ])
    sleeps = []
    monkeypatch.setattr(task_runner_module.random, "uniform", lambda _low, _high: 0)
    monkeypatch.setattr(task_runner_module.time, "sleep", sleeps.append)

    response = TaskRunner._invoke_with_rate_limit_retry(client, object(), {})

    assert response["ErrorCode"] == 429
    assert len(client.calls) == 4
    assert sleeps == [0.2, 0.4, 0.8]


def test_rate_limit_retry_api3_request_limit_exceeded(monkeypatch):
    client = _FakeClient([
        {"ActionStatus": "FAIL", "ErrorCode": "RequestLimitExceeded"},
        {"ActionStatus": "OK", "ErrorCode": 0},
    ])
    sleeps = []
    monkeypatch.setattr(task_runner_module.random, "uniform", lambda _low, _high: 0)
    monkeypatch.setattr(task_runner_module.time, "sleep", sleeps.append)

    response = TaskRunner._invoke_with_rate_limit_retry(client, object(), {})

    assert response["ActionStatus"] == "OK"
    assert len(client.calls) == 2
    assert sleeps == [0.2]


def test_worker_counts_retried_item_once(monkeypatch):
    api_meta = SimpleNamespace(
        name="测试接口",
        service_type="im",
        build_payload=lambda row: row,
    )
    responses = iter([
        {"ActionStatus": "FAIL", "ErrorCode": "90001", "ErrorInfo": "频率超限"},
        {"ActionStatus": "OK", "ErrorCode": 0},
    ])
    monkeypatch.setattr(task_runner_module.registry, "get_api", lambda _api_id: api_meta)
    monkeypatch.setattr(
        task_runner_module.UniversalCloudClient,
        "invoke_api",
        lambda _self, _api, _payload: next(responses),
    )
    monkeypatch.setattr(task_runner_module, "save_history_item", lambda _record: None)
    monkeypatch.setattr(task_runner_module, "RATE_LIMIT_BACKOFF_BASE_SEC", 0)
    monkeypatch.setattr(task_runner_module, "RATE_LIMIT_BACKOFF_JITTER_SEC", 0)

    runner = TaskRunner()
    task = runner.create_task("test_api", [{"id": 1}], delay_sec=0)
    runner._worker(task)

    assert task.status == "COMPLETED"
    assert task.success == 1
    assert task.failed == 0
    assert task.results[0]["_status"] == "OK"
