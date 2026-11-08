import json
import os
import sqlite3
import time
from types import SimpleNamespace

import pytest


def test_unified_sqlite_schema_is_created_with_safe_pragmas(tmp_path):
    from core.persistence import open_database

    database = tmp_path / "tim.sqlite3"
    connection = open_database(str(database))
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "schema_migrations",
            "idempotency_keys",
            "batch_tasks",
            "audit_events",
            "orchestration_events",
        } <= tables
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    finally:
        connection.close()


def test_legacy_idempotency_database_is_imported_without_deleting_source(tmp_path):
    from core.persistence import open_database

    legacy = tmp_path / "batch_idempotency.sqlite3"
    legacy_connection = sqlite3.connect(legacy)
    try:
        legacy_connection.execute(
            """
            CREATE TABLE batch_idempotency (
                owner_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                task_id TEXT NOT NULL,
                api_id TEXT NOT NULL,
                total INTEGER NOT NULL,
                delay_sec REAL NOT NULL,
                dry_run INTEGER NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (owner_id, idempotency_key)
            )
            """
        )
        created_at = time.time()
        legacy_connection.execute(
            """
            INSERT INTO batch_idempotency
                (owner_id, idempotency_key, request_hash, task_id, api_id,
                 total, delay_sec, dry_run, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("alice", "legacy-key", "hash-1", "task-1", "c2c_msg_withdraw", 1, 0, 1, created_at),
        )
        legacy_connection.commit()
    finally:
        legacy_connection.close()

    target = tmp_path / "tim.sqlite3"
    connection = open_database(str(target))
    try:
        row = connection.execute(
            """
            SELECT owner_subject, idempotency_key, task_id, operation_kind, state
            FROM idempotency_keys
            """
        ).fetchone()
        assert tuple(row) == ("alice", "legacy-key", "task-1", "batch", "RESERVED")
    finally:
        connection.close()

    assert legacy.exists()


def test_task_runner_idempotency_uses_unified_schema(tmp_path, monkeypatch):
    import core.task_runner as task_runner_module

    database = tmp_path / "tim.sqlite3"
    monkeypatch.setattr(task_runner_module, "IDEMPOTENCY_DB_PATH", str(database))
    monkeypatch.setattr(task_runner_module, "STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setattr(task_runner_module, "RESULTS_DIR", str(tmp_path / "storage" / "batch_results"))

    runner = task_runner_module.TaskRunner()
    task = runner.create_task(
        "c2c_msg_withdraw",
        [{"id": "1"}],
        delay_sec=0,
        dry_run=True,
        owner_id="alice",
        idempotency_key="unified-key",
        request_fingerprint="hash-1",
    )

    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            """
            SELECT owner_subject, operation_kind, idempotency_key, task_id
            FROM idempotency_keys
            """
        ).fetchone()
        assert row == ("alice", "batch", "unified-key", task.task_id)
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'batch_idempotency'"
        ).fetchone()
    finally:
        connection.close()


def test_batch_task_reservation_and_events_survive_runner_restart(tmp_path, monkeypatch):
    import core.task_runner as task_runner_module

    database = tmp_path / "tim.sqlite3"
    monkeypatch.setattr(task_runner_module, "IDEMPOTENCY_DB_PATH", str(database))
    monkeypatch.setattr(task_runner_module, "STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setattr(
        task_runner_module,
        "RESULTS_DIR",
        str(tmp_path / "storage" / "batch_results"),
    )

    first_runner = task_runner_module.TaskRunner()
    task = first_runner.create_task(
        "c2c_msg_withdraw",
        [{"id": "1"}],
        delay_sec=0,
        dry_run=True,
        owner_id="alice",
    )
    assert task_runner_module.SqliteTaskStore(str(database)).get_batch_task(
        task.task_id
    )["status"] == "RESERVED"

    second_runner = task_runner_module.TaskRunner()
    recovered = second_runner.get_task(task.task_id)
    assert recovered is not None
    assert recovered.status == "RESERVED"
    assert recovered.items == [{"id": "1"}]

    assert second_runner.start_task(task.task_id) is True
    deadline = time.time() + 2
    while recovered.end_time is None and time.time() < deadline:
        time.sleep(0.01)

    record = second_runner.get_task_record(task.task_id)
    assert record["status"] == "COMPLETED"
    assert record["current_index"] == 1
    assert [event[1]["type"] for event in second_runner.list_task_events(task.task_id)] == [
        "start",
        "progress",
        "complete",
    ]


def test_stale_running_task_becomes_unknown_and_is_not_automatically_replayed(
    tmp_path,
):
    from core.persistence import SqliteTaskStore

    database = tmp_path / "tim.sqlite3"
    store = SqliteTaskStore(str(database))
    now = time.time()
    assert store.create_batch_task(
        {
            "task_id": "stale-task",
            "owner_subject": "alice",
            "api_id": "c2c_msg_withdraw",
            "status": "RUNNING",
            "total": 1,
            "items_json": '[{"id":"1"}]',
            "worker_id": "dead-worker",
            "lease_until": now - 1,
            "last_heartbeat": now - 61,
        }
    )
    assert store.claim_batch_task("stale-task", "new-worker") is False
    record = store.get_batch_task("stale-task")
    assert record["status"] == "UNKNOWN"
    assert record["worker_id"] is None
    assert record["finished_at"] is not None
    assert "unknown" in record["error_message"].lower()
    assert (
        store.set_task_terminal(
            "stale-task",
            status="COMPLETED",
            worker_id="dead-worker",
            current_index=1,
            success_count=1,
            failed_count=0,
        )
        is False
    )
    assert store.get_batch_task("stale-task")["status"] == "UNKNOWN"


def test_remote_stop_sets_terminal_time_and_clears_idempotency_lease(tmp_path):
    from core.persistence import SqliteTaskStore, open_database

    database = tmp_path / "tim.sqlite3"
    store = SqliteTaskStore(str(database))
    now = time.time()
    assert store.create_batch_task(
        {
            "task_id": "stop-task",
            "owner_subject": "alice",
            "api_id": "c2c_msg_withdraw",
            "status": "RUNNING",
            "total": 1,
            "worker_id": "worker-a",
            "lease_until": now + 60,
            "last_heartbeat": now,
        }
    )
    connection = open_database(str(database))
    try:
        connection.execute(
            """
            INSERT INTO idempotency_keys (
                issuer, tenant_id, owner_subject, operation_kind,
                idempotency_key, request_hash, task_id, api_id, total,
                delay_sec, dry_run, state, created_at, updated_at,
                lease_until, worker_id
            ) VALUES ('', '', 'alice', 'batch', 'stop-key', 'hash',
                      'stop-task', 'c2c_msg_withdraw', 1, 0, 0, 'RUNNING',
                      ?, ?, ?, ?)
            """,
            (now, now, now + 60, "worker-a"),
        )
        connection.commit()
    finally:
        connection.close()

    result = store.request_control("stop-task", "stop")
    assert result["status"] == "CANCELLED"
    assert result["finished_at"] is not None
    assert result["worker_id"] is None
    assert result["lease_until"] is None

    connection = open_database(str(database))
    try:
        row = connection.execute(
            """
            SELECT state, worker_id, lease_until, finished_at
            FROM idempotency_keys
            WHERE task_id = 'stop-task'
            """
        ).fetchone()
        assert tuple(row) == ("CANCELLED", None, None, result["finished_at"])
    finally:
        connection.close()


def test_stale_terminal_writer_cannot_overwrite_remote_cancel(tmp_path):
    from core.persistence import SqliteTaskStore

    database = tmp_path / "tim.sqlite3"
    store = SqliteTaskStore(str(database))
    now = time.time()
    store.create_batch_task(
        {
            "task_id": "race-task",
            "owner_subject": "alice",
            "api_id": "c2c_msg_withdraw",
            "status": "RUNNING",
            "total": 1,
            "worker_id": "worker-a",
            "lease_until": now + 60,
        }
    )

    assert store.request_control("race-task", "stop")["status"] == "CANCELLED"
    assert (
        store.set_task_terminal(
            "race-task",
            status="COMPLETED",
            worker_id="worker-a",
            current_index=1,
            success_count=1,
            failed_count=0,
        )
        is False
    )
    record = store.get_batch_task("race-task")
    assert record["status"] == "CANCELLED"
    assert record["finished_at"] is not None


def test_runner_finalization_adopts_remote_cancel(tmp_path, monkeypatch):
    import core.task_runner as task_runner_module

    database = tmp_path / "tim.sqlite3"
    monkeypatch.setattr(task_runner_module, "IDEMPOTENCY_DB_PATH", str(database))
    monkeypatch.setattr(task_runner_module, "STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setattr(
        task_runner_module,
        "RESULTS_DIR",
        str(tmp_path / "storage" / "batch_results"),
    )
    saved = []
    monkeypatch.setattr(task_runner_module, "save_history_item", saved.append)

    runner = task_runner_module.TaskRunner()
    task = task_runner_module.BatchTask(
        "finalize-race",
        "c2c_msg_withdraw",
        [{"id": "1"}],
        database_path_override=str(database),
    )
    task.worker_id = runner.worker_id
    task.status = "RUNNING"
    task.start_time = time.time()
    task._started = True
    store = task._store
    store.create_batch_task(
        {
            "task_id": task.task_id,
            "owner_subject": "alice",
            "api_id": task.api_id,
            "status": "RUNNING",
            "total": 1,
            "items_json": '[{"id":"1"}]',
            "worker_id": runner.worker_id,
            "lease_until": time.time() + 60,
            "started_at": task.start_time,
        }
    )

    original_terminal = store.set_task_terminal

    def stop_before_terminal(*args, **kwargs):
        assert store.request_control(task.task_id, "stop")["status"] == "CANCELLED"
        return original_terminal(*args, **kwargs)

    monkeypatch.setattr(store, "set_task_terminal", stop_before_terminal)
    runner._finalize_task(
        task,
        SimpleNamespace(name="测试接口", service_type="im"),
    )

    assert task.status == "CANCELLED"
    assert store.get_batch_task(task.task_id)["status"] == "CANCELLED"
    assert saved and saved[0]["status"] == "CANCELLED"


def test_clear_history_preserves_active_and_unknown_tasks(tmp_path, monkeypatch):
    import core.task_runner as task_runner_module

    database = tmp_path / "tim.sqlite3"
    storage = tmp_path / "storage"
    results = storage / "batch_results"
    results.mkdir(parents=True)
    history_path = storage / "history.json"
    monkeypatch.setattr(task_runner_module, "IDEMPOTENCY_DB_PATH", str(database))
    monkeypatch.setattr(task_runner_module, "STORAGE_DIR", str(storage))
    monkeypatch.setattr(task_runner_module, "HISTORY_PATH", str(history_path))
    monkeypatch.setattr(task_runner_module, "RESULTS_DIR", str(results))

    store = task_runner_module.SqliteTaskStore(str(database))
    for task_id, status in (
        ("completed-task", "COMPLETED"),
        ("running-task", "RUNNING"),
        ("unknown-task", "UNKNOWN"),
    ):
        store.create_batch_task(
            {
                "task_id": task_id,
                "owner_subject": "alice",
                "api_id": "c2c_msg_withdraw",
                "status": status,
                "total": 1,
                "items_json": '[{"id":"1"}]',
            }
        )
        (results / f"task_{task_id}.jsonl").write_text("{}\n", encoding="utf-8")

    history_path.write_text(
        json.dumps(
            [
                {"task_id": "completed-task", "status": "COMPLETED"},
                {"task_id": "running-task", "status": "RUNNING"},
                {"task_id": "unknown-task", "status": "UNKNOWN"},
            ]
        ),
        encoding="utf-8",
    )

    assert task_runner_module.clear_history() is True
    assert store.get_batch_task("completed-task") is None
    assert store.get_batch_task("running-task")["status"] == "RUNNING"
    assert store.get_batch_task("unknown-task")["status"] == "UNKNOWN"
    assert not (results / "task_completed-task.jsonl").exists()
    assert (results / "task_running-task.jsonl").exists()
    assert (results / "task_unknown-task.jsonl").exists()
    assert {
        item["task_id"]
        for item in json.loads(history_path.read_text(encoding="utf-8"))
    } == {"running-task", "unknown-task"}
    assert store.delete_batch_task("running-task") is False


def test_sync_job_status_and_idempotency_are_shared_by_managers(tmp_path):
    import threading

    from core.sync_task_manager import SyncTaskManager

    database = tmp_path / "tim.sqlite3"
    first = SyncTaskManager(database_path=str(database), persistent=True)
    second = SyncTaskManager(database_path=str(database), persistent=True)
    release = threading.Event()

    def sync_operation():
        assert release.wait(2.0)
        return {"ok": True}

    started = first.start(
        "docs_tree",
        sync_operation,
        owner_id="alice",
        idempotency_key="sync-1",
    )
    assert started["status"] == "started"
    task_id = started["task_id"]

    replay = second.start(
        "docs_tree",
        lambda: {"must-not-run": True},
        owner_id="alice",
        idempotency_key="sync-1",
    )
    assert replay["status"] == "idempotent_replay"
    assert replay["task_id"] == task_id

    cross_process_status = second.get_status(
        "docs_tree",
        task_id,
        owner_id="alice",
    )
    assert cross_process_status["task_id"] == task_id
    assert cross_process_status["status"] in {"PENDING", "RUNNING"}

    release.set()
    deadline = time.time() + 2
    while time.time() < deadline:
        status = second.get_status("docs_tree", task_id, owner_id="alice")
        if status["status"] == "COMPLETED":
            break
        time.sleep(0.01)
    assert status["status"] == "COMPLETED"
    assert status["result"] == {"ok": True}


def test_sync_job_expired_lease_becomes_unknown_and_old_worker_cannot_finish(tmp_path):
    from core.persistence import SqliteSyncJobStore

    database = tmp_path / "tim.sqlite3"
    store = SqliteSyncJobStore(str(database))
    reservation = store.reserve(
        task_id="sync-stale",
        kind="docs_tree",
        owner_subject="alice",
        idempotency_key="stale-key",
        is_admin=False,
    )
    assert reservation["status"] == "started"
    assert store.claim("sync-stale", "dead-worker", lease_seconds=1) is True

    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE sync_jobs SET lease_until = ? WHERE task_id = ?",
            (time.time() - 1, "sync-stale"),
        )
        connection.commit()
    finally:
        connection.close()

    row = store.get("sync-stale")
    assert row["status"] == "UNKNOWN"
    assert row["worker_id"] is None
    assert row["finished_at"] is not None
    assert (
        store.set_terminal(
            "sync-stale",
            worker_id="dead-worker",
            status="COMPLETED",
            result_json='{"must_not_publish":true}',
        )
        is False
    )
    assert store.get("sync-stale")["status"] == "UNKNOWN"


def test_batch_idempotency_isolated_by_issuer_and_tenant(tmp_path, monkeypatch):
    import core.task_runner as task_runner_module

    database = tmp_path / "tim.sqlite3"
    monkeypatch.setattr(task_runner_module, "IDEMPOTENCY_DB_PATH", str(database))
    monkeypatch.setattr(task_runner_module, "STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setattr(
        task_runner_module,
        "RESULTS_DIR",
        str(tmp_path / "storage" / "batch_results"),
    )
    first = task_runner_module.TaskRunner()
    second = task_runner_module.TaskRunner()

    task_a = first.create_task(
        "c2c_msg_withdraw",
        [{"id": "a"}],
        delay_sec=0,
        dry_run=True,
        owner_id="same-subject",
        issuer="https://issuer-a.example",
        tenant_id="tenant-a",
        idempotency_key="same-key",
    )
    task_b = second.create_task(
        "c2c_msg_withdraw",
        [{"id": "b"}],
        delay_sec=0,
        dry_run=True,
        owner_id="same-subject",
        issuer="https://issuer-b.example",
        tenant_id="tenant-b",
        idempotency_key="same-key",
    )
    assert task_a.task_id != task_b.task_id
    assert first.get_task_record(task_a.task_id)["issuer"] == "https://issuer-a.example"
    assert second.get_task_record(task_b.task_id)["tenant_id"] == "tenant-b"


def test_batch_result_index_tracks_jsonl_offsets_and_hashes(tmp_path, monkeypatch):
    import core.task_runner as task_runner_module
    from core.persistence import SqliteTaskStore

    database = tmp_path / "tim.sqlite3"
    monkeypatch.setattr(task_runner_module, "IDEMPOTENCY_DB_PATH", str(database))
    monkeypatch.setattr(task_runner_module, "RESULTS_DIR", str(tmp_path / "results"))

    path = task_runner_module.append_task_result(
        "indexed-task",
        {"id": 1, "_status": "OK", "_err_code": 0},
    )
    task_runner_module.append_task_result(
        "indexed-task",
        {"id": 2, "_status": "FAIL", "_err_code": 90001},
    )
    store = SqliteTaskStore(str(database))
    rows = store.list_batch_task_items("indexed-task")
    assert len(rows) == 2
    assert rows[0]["sequence"] == 0
    assert rows[1]["sequence"] == 1
    assert all(row["result_uri"] == path for row in rows)
    assert all(len(row["result_sha256"]) == 64 for row in rows)
    assert task_runner_module.task_result_consistency("indexed-task") == {
        "file_exists": True,
        "indexed_count": 2,
        "file_count": 2,
        "consistent": True,
    }


def test_batch_result_consistency_rejects_truncate_tamper_delete_and_permission(
    tmp_path,
    monkeypatch,
):
    import core.task_runner as task_runner_module

    database = tmp_path / "tim.sqlite3"
    results_dir = tmp_path / "results"
    monkeypatch.setattr(task_runner_module, "IDEMPOTENCY_DB_PATH", str(database))
    monkeypatch.setattr(task_runner_module, "RESULTS_DIR", str(results_dir))

    path = task_runner_module.append_task_result(
        "fault-task",
        {"id": 1, "_status": "OK", "_err_code": 0},
    )
    result_path = os.path.abspath(path)
    original = open(result_path, "rb").read()
    assert task_runner_module.task_result_consistency("fault-task")["consistent"]

    with open(result_path, "wb") as handle:
        handle.write(
            original.replace(
                b'"id":1',
                b'"id":2',
                1,
            )
        )
    tampered = task_runner_module.task_result_consistency("fault-task")
    assert tampered["consistent"] is False
    assert "哈希" in tampered["error"]

    with open(result_path, "wb") as handle:
        handle.write(original[:-1])
    truncated = task_runner_module.task_result_consistency("fault-task")
    assert truncated["consistent"] is False
    assert truncated["error"] in {
        "结果文件被截断",
        "结果文件存在未完成的 JSONL 行",
    }

    os.remove(result_path)
    missing = task_runner_module.task_result_consistency("fault-task")
    assert missing["consistent"] is False
    assert missing["error"] == "结果文件缺失"

    with open(result_path, "wb") as handle:
        handle.write(original)
    os.chmod(result_path, 0)
    try:
        unreadable = task_runner_module.task_result_consistency("fault-task")
    finally:
        os.chmod(result_path, 0o600)
    assert unreadable["consistent"] is False
    assert unreadable["error"].startswith("结果文件不可读:")


def test_batch_export_rejects_missing_indexed_result_file(tmp_path, monkeypatch):
    import core.task_runner as task_runner_module
    import app as app_module
    from fastapi import HTTPException

    database = tmp_path / "tim.sqlite3"
    results_dir = tmp_path / "results"
    monkeypatch.setattr(task_runner_module, "IDEMPOTENCY_DB_PATH", str(database))
    monkeypatch.setattr(task_runner_module, "RESULTS_DIR", str(results_dir))

    path = task_runner_module.append_task_result(
        "export-fault-task",
        {"id": 1, "_status": "OK", "_err_code": 0},
    )
    os.remove(path)
    with pytest.raises(HTTPException) as exc_info:
        app_module.export_csv("export-fault-task")
    assert exc_info.value.status_code == 409
    assert "结果文件已丢失" in str(exc_info.value.detail)


def test_batch_result_index_failure_rolls_back_file_and_raises(
    tmp_path,
    monkeypatch,
):
    import core.task_runner as task_runner_module
    from core.persistence import SqliteTaskStore

    database = tmp_path / "tim.sqlite3"
    results_dir = tmp_path / "results"
    monkeypatch.setattr(task_runner_module, "IDEMPOTENCY_DB_PATH", str(database))
    monkeypatch.setattr(task_runner_module, "RESULTS_DIR", str(results_dir))

    def fail_index(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SqliteTaskStore, "append_batch_task_item", fail_index)
    with pytest.raises(task_runner_module.ResultPersistenceError):
        task_runner_module.append_task_result(
            "index-failure-task",
            {"id": 1, "_status": "OK", "_err_code": 0},
        )
    result_path = results_dir / "task_index-failure-task.jsonl"
    assert not result_path.exists() or result_path.read_bytes() == b""


def test_audit_metadata_redacts_credentials(tmp_path):
    from core.persistence import SqliteAuditStore

    store = SqliteAuditStore(str(tmp_path / "tim.sqlite3"))
    event_id = store.append(
        action="credential.update",
        resource_type="environment",
        resource_id="env-1",
        actor_subject="alice",
        metadata={
            "secret_key": "real-secret",
            "nested": {"Authorization": "TC3-HMAC-SHA256 secret"},
            "count": 2,
        },
    )
    assert event_id
    rows = store.list_recent(limit=1)
    payload = json.loads(rows[0]["metadata_json"])
    assert payload["secret_key"] == "[redacted]"
    assert payload["nested"]["Authorization"] == "[redacted]"
    assert payload["count"] == 2
