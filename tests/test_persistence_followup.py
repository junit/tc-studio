from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
import threading
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_app_settings_are_shared_and_audited(tmp_path):
    from core.agent.bot_manager import BotManager
    from core.agent.models import LLMConfig
    from core.persistence import SqliteAppSettingsStore

    data_dir = tmp_path / "data"
    first = BotManager(data_dir=str(data_dir))
    second = BotManager(data_dir=str(data_dir))

    assert first.set_high_risk_keywords(
        ["DeleteGroup", "  deletegroup  ", "ShutdownLive"],
        actor_subject="admin-1",
        issuer="local",
        tenant_id="tenant-a",
    ) == ["deletegroup", "shutdownlive"]
    assert second.get_high_risk_keywords() == ["deletegroup", "shutdownlive"]

    setting = SqliteAppSettingsStore(str(data_dir / "tc_studio.sqlite3")).get(
        "security.high_risk_keywords"
    )
    assert setting is not None
    assert setting["version"] == 1

    connection = sqlite3.connect(data_dir / "tc_studio.sqlite3")
    try:
        audit = connection.execute(
            """
            SELECT action, actor_subject, issuer, tenant_id
            FROM audit_events
            WHERE resource_id = 'security.high_risk_keywords'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        connection.close()
    assert audit == (
        "security.high_risk_keywords.update",
        "admin-1",
        "local",
        "tenant-a",
    )

    first.save_llm_config(
        LLMConfig(provider="openai", model_name="model", api_key="unit-" + "test-key")
    )
    assert stat.S_IMODE(os.stat(data_dir / "llm_config.json").st_mode) == 0o600


def test_history_sqlite_is_authoritative_and_deleted_rows_cannot_resurrect(
    tmp_path,
    monkeypatch,
):
    import core.task_runner as task_runner

    database = tmp_path / "tim.sqlite3"
    storage = tmp_path / "storage"
    results = storage / "batch_results"
    history = storage / "history.json"
    results.mkdir(parents=True)
    monkeypatch.setattr(task_runner, "IDEMPOTENCY_DB_PATH", str(database))
    monkeypatch.setattr(task_runner, "STORAGE_DIR", str(storage))
    monkeypatch.setattr(task_runner, "RESULTS_DIR", str(results))
    monkeypatch.setattr(task_runner, "HISTORY_PATH", str(history))

    history.write_text(
        json.dumps(
            [
                {
                    "task_id": "legacy-task",
                    "api_id": "legacy_api",
                    "status": "COMPLETED",
                    "total": 1,
                    "success": 1,
                }
            ]
        ),
        encoding="utf-8",
    )
    assert task_runner.load_history()[0]["status"] == "COMPLETED"

    store = task_runner.SqliteTaskStore(str(database))
    assert store.update_batch_task(
        "legacy-task",
        status="FAILED",
        success_count=0,
        failed_count=1,
    )
    assert task_runner.load_history()[0]["status"] == "FAILED"

    assert task_runner.delete_history_item("legacy-task") is True
    assert store.get_batch_task("legacy-task") is None
    assert task_runner.load_history() == []
    assert json.loads(history.read_text(encoding="utf-8")) == []


def test_save_history_item_does_not_create_legacy_json(tmp_path, monkeypatch):
    import core.task_runner as task_runner

    database = tmp_path / "tim.sqlite3"
    storage = tmp_path / "storage"
    history = storage / "history.json"
    monkeypatch.setattr(task_runner, "IDEMPOTENCY_DB_PATH", str(database))
    monkeypatch.setattr(task_runner, "STORAGE_DIR", str(storage))
    monkeypatch.setattr(task_runner, "RESULTS_DIR", str(storage / "batch_results"))
    monkeypatch.setattr(task_runner, "HISTORY_PATH", str(history))

    task_runner.save_history_item(
        {
            "task_id": "sqlite-only",
            "api_id": "test_api",
            "status": "COMPLETED",
            "total": 0,
        }
    )
    assert not history.exists()
    assert task_runner.SqliteTaskStore(str(database)).get_batch_task("sqlite-only")


def test_document_sync_progress_and_cancel_are_shared_between_managers(
    tmp_path,
    monkeypatch,
):
    import core.doc_syncer as doc_syncer
    from core.doc_syncer import DocSyncManager
    from core.sync_task_manager import SyncTaskManager

    database = tmp_path / "tim.sqlite3"
    first_manager = DocSyncManager(
        SyncTaskManager(database_path=str(database), persistent=True)
    )
    second_manager = DocSyncManager(
        SyncTaskManager(database_path=str(database), persistent=True)
    )
    started = threading.Event()
    release = threading.Event()

    def parse(doc_id, api_id="", doc_url="", mode="auto"):
        started.set()
        assert release.wait(2.0)
        return {"doc_id": doc_id, "api_id": api_id, "status": "synced"}

    monkeypatch.setattr(doc_syncer, "parse_official_doc", parse)
    monkeypatch.setattr(doc_syncer, "save_doc", lambda value: str(value["doc_id"]))

    started_result = first_manager.start_batch_sync(
        [
            {"doc_id": 101, "api_id": "api-101", "name": "A"},
            {"doc_id": 102, "api_id": "api-102", "name": "B"},
        ],
        force=True,
    )
    task_id = started_result["task_id"]
    assert started.wait(1.0)

    shared = second_manager.get_status()
    assert shared["task_id"] == task_id
    assert shared["total"] == 2
    assert shared["status"] in {"PENDING", "RUNNING"}

    cancel = second_manager.cancel_batch_sync()
    assert cancel["status"] == "cancelling"
    release.set()

    deadline = time.time() + 3
    final = second_manager.get_status()
    while final["status"] not in {"CANCELLED", "UNKNOWN"} and time.time() < deadline:
        time.sleep(0.02)
        final = second_manager.get_status()
    assert final["status"] == "CANCELLED"
    assert final["is_running"] is False


def test_cancelled_sync_can_be_replaced_and_pending_cancel_does_not_claim(
    tmp_path,
):
    from core.persistence import SqliteSyncJobStore

    database = tmp_path / "tim.sqlite3"
    store = SqliteSyncJobStore(str(database))
    first = store.reserve(
        task_id="sync-cancelled",
        kind="docs_batch",
        owner_subject="alice",
        idempotency_key="cancelled-key",
        is_admin=False,
    )
    assert first["status"] == "started"
    cancelled = store.request_cancel("sync-cancelled")
    assert cancelled is not None
    assert cancelled["status"] == "CANCELLED"
    assert store.claim("sync-cancelled", "late-worker") is False

    replacement = store.reserve(
        task_id="sync-replacement",
        kind="docs_batch",
        owner_subject="alice",
        idempotency_key="replacement-key",
        is_admin=False,
    )
    assert replacement["status"] == "started"
    assert replacement["task_id"] == "sync-replacement"


def test_nonpersistent_sync_operation_factory_is_used(tmp_path):
    from core.sync_task_manager import SyncTaskManager

    manager = SyncTaskManager()
    called = threading.Event()

    def operation_factory(_task_id):
        def operation():
            called.set()
            return {"ok": True}

        return operation

    started = manager.start(
        "docs_batch",
        lambda: {"wrong": True},
        operation_factory=operation_factory,
        initial_progress={"total": 1, "completed": 0},
        owner_id="alice",
    )
    assert called.wait(1.0)
    final = manager.get_status(
        "docs_batch",
        started["task_id"],
        owner_id="alice",
    )
    assert final["status"] == "COMPLETED"
    assert final["result"] == {"ok": True}


def test_orchestration_events_are_sqlite_first_and_legacy_import_is_one_way(
    tmp_path,
):
    from core.agent.bot_manager import BotManager

    data_dir = tmp_path / "data"
    first = BotManager(data_dir=str(data_dir))
    second = BotManager(data_dir=str(data_dir))
    first.record_orchestration(
        {
            "caller_bot_id": "default_assistant",
            "target_bot_id": "risk_inspector",
            "task": "跨实例事件",
            "status": "success",
            "owner_id": "alice",
        }
    )
    assert not (data_dir / "orchestrations.json").exists()
    visible = second.get_orchestrations(
        limit=10,
        user_id="alice",
        is_admin=False,
    )
    assert visible and visible[0]["task"] == "跨实例事件"

    legacy = data_dir / "orchestrations.json"
    legacy.write_text(
        json.dumps(
            [
                {
                    "caller_bot_id": "default_assistant",
                    "target_bot_id": "css_live_operator",
                    "task": "旧事件导入",
                    "status": "success",
                    "owner_id": "bob",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    imported = second.get_orchestrations(limit=10, is_admin=True)
    assert imported[0]["task"] == "跨实例事件"
    assert all(item["task"] != "旧事件导入" for item in imported)

    # A non-empty SQLite table prevents a late legacy snapshot from
    # resurrecting records that were already deleted or superseded.
    connection = sqlite3.connect(first.database_path)
    try:
        connection.execute("DELETE FROM orchestration_events")
        connection.commit()
    finally:
        connection.close()
    imported_after_empty = second.get_orchestrations(limit=10, is_admin=True)
    assert imported_after_empty[0]["task"] == "旧事件导入"


def test_atomic_json_publish_keeps_previous_document_on_serialization_failure(tmp_path):
    from core.file_storage import atomic_write_json

    target = tmp_path / "catalog.json"
    atomic_write_json(target, {"version": 1}, mode=0o600)
    try:
        atomic_write_json(target, {"bad": object()}, mode=0o600)
    except TypeError:
        pass
    assert json.loads(target.read_text(encoding="utf-8")) == {"version": 1}
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
