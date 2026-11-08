from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def _set_scope(user):
    from core.runtime_context import runtime_auth_scope, runtime_user_id

    return (
        runtime_auth_scope.set(user.auth_scope),
        runtime_user_id.set(user.subject),
    )


def _reset_scope(tokens):
    from core.runtime_context import runtime_auth_scope, runtime_user_id

    scope_token, user_token = tokens
    runtime_user_id.reset(user_token)
    runtime_auth_scope.reset(scope_token)


def test_agent_state_isolated_by_issuer_and_tenant(tmp_path, monkeypatch):
    import apis.agent as agent_api
    from core.agent.bot_manager import BotManager
    from core.auth import CurrentUser

    manager = BotManager(data_dir=str(tmp_path / "data"))
    monkeypatch.setattr(agent_api, "bot_manager", manager)
    tenant_a = CurrentUser(
        "same-subject",
        claims={"iss": "https://issuer-a.example", "tenant_id": "tenant-a"},
    )
    tenant_b = CurrentUser(
        "same-subject",
        claims={"iss": "https://issuer-b.example", "tenant_id": "tenant-b"},
    )

    tokens = _set_scope(tenant_a)
    try:
        bot = manager.create_bot(
            "scoped-private",
            "Scoped private",
            owner_id=tenant_a.subject,
            issuer=tenant_a.issuer,
            tenant_id=tenant_a.tenant_id,
        )
        manager.append_history(bot.id, "user", "tenant-a secret", user_id=tenant_a.subject)
        assert agent_api.require_bot_access(bot.id, tenant_a).subject == tenant_a.subject
        assert manager.get_history(bot.id, user_id=tenant_a.subject)[0]["content"] == "tenant-a secret"
    finally:
        _reset_scope(tokens)

    with pytest.raises(HTTPException) as exc_info:
        agent_api.require_bot_access(bot.id, tenant_b)
    assert exc_info.value.status_code == 404

    tokens = _set_scope(tenant_b)
    try:
        assert manager.get_history(bot.id, user_id=tenant_b.subject) == []
        assert all(item["id"] != bot.id for item in manager.list_bots(user_id=tenant_b.subject))
    finally:
        _reset_scope(tokens)


def test_call_agent_explicit_id_cannot_cross_issuer_or_tenant(tmp_path):
    from core.agent.bot_manager import BotManager
    from core.agent.tools import CallAgentTool
    from core.auth import CurrentUser

    manager = BotManager(data_dir=str(tmp_path / "data"))
    tenant_a = CurrentUser(
        "same-subject",
        claims={"iss": "https://issuer-a.example", "tenant_id": "tenant-a"},
    )
    tenant_b = CurrentUser(
        "same-subject",
        claims={"iss": "https://issuer-b.example", "tenant_id": "tenant-b"},
    )

    tokens = _set_scope(tenant_a)
    try:
        target = manager.create_bot(
            "target-a",
            "Target A",
            owner_id=tenant_a.subject,
            issuer=tenant_a.issuer,
            tenant_id=tenant_a.tenant_id,
        )
    finally:
        _reset_scope(tokens)

    tokens = _set_scope(tenant_b)
    try:
        caller = manager.create_bot(
            "caller-b",
            "Caller B",
            owner_id=tenant_b.subject,
            issuer=tenant_b.issuer,
            tenant_id=tenant_b.tenant_id,
        )
        called: list[str] = []

        class StubEngine:
            def execute_subtask(self, **kwargs):
                called.append(kwargs["target_bot_id"])
                return "unexpected"

        tool = CallAgentTool(caller.id, manager, engine=StubEngine())
        result = tool.forward(
            f"Target A ({target.id}) suffix",
            "cross-tenant probe",
        )
    finally:
        _reset_scope(tokens)

    assert called == []
    assert "未能" in result

    from core.agent.engine import AgentEngine

    tokens = _set_scope(tenant_b)
    try:
        engine = AgentEngine.__new__(AgentEngine)
        engine.manager = manager
        with pytest.raises(PermissionError, match="当前认证范围"):
            engine.execute_subtask(
                caller_bot_id="caller-b",
                target_bot_id=target.id,
                task="direct boundary probe",
            )
    finally:
        _reset_scope(tokens)


def test_admin_bot_maintenance_is_scope_bound_and_audited(tmp_path, monkeypatch):
    from apis import agent as agent_api
    from app import app
    from core import auth
    from core.agent.bot_manager import BotManager
    from core.auth import CurrentUser

    manager = BotManager(data_dir=str(tmp_path / "data"))
    source = CurrentUser(
        "owner-a",
        claims={"iss": "https://issuer-a.example", "tenant_id": "tenant-a"},
    )
    bot = manager.create_bot(
        "tenant-a-admin-target",
        "Tenant A target",
        owner_id=source.subject,
        issuer=source.issuer,
        tenant_id=source.tenant_id,
    )
    admin_b = CurrentUser(
        "admin-b",
        frozenset({"admin"}),
        claims={"iss": "https://issuer-b.example", "tenant_id": "tenant-b"},
    )
    monkeypatch.setattr(agent_api, "bot_manager", manager)
    app.dependency_overrides[auth.current_user] = lambda: admin_b
    app.dependency_overrides[agent_api.current_user] = lambda: admin_b
    try:
        client = TestClient(app)
        transfer = client.post(
            f"/api/agent/bots/{bot.id}/ownership",
            json={"owner_id": "attacker-b"},
        )
        migrate = client.post(
            f"/api/agent/bots/{bot.id}/legacy-state/migrate",
            json={"target_user_id": "attacker-b"},
        )
    finally:
        app.dependency_overrides.pop(auth.current_user, None)
        app.dependency_overrides.pop(agent_api.current_user, None)

    assert transfer.status_code == 404
    assert migrate.status_code == 404
    assert manager.get_bot(bot.id).owner_id == source.subject
    assert manager.audit_store.list_recent(
        issuer=source.issuer,
        tenant_id=source.tenant_id,
        limit=20,
    ) == []


def test_rag_documents_are_scope_bound_for_reads_and_writes(tmp_path, monkeypatch):
    from core.agent import bot_manager as bot_module
    from core.agent.bot_manager import BotManager
    from core.agent.rag_manager import RagManager
    from core.auth import CurrentUser

    manager = BotManager(data_dir=str(tmp_path / "data"))
    monkeypatch.setattr(bot_module, "bot_manager", manager)
    rag = RagManager(data_root=str(tmp_path / "data" / "bots"))
    tenant_a = CurrentUser(
        "same-subject",
        claims={"iss": "https://issuer-a.example", "tenant_id": "tenant-a"},
    )
    tenant_b = CurrentUser(
        "same-subject",
        claims={"iss": "https://issuer-b.example", "tenant_id": "tenant-b"},
    )

    tokens = _set_scope(tenant_a)
    try:
        bot = manager.create_bot(
            "rag-scoped-a",
            "RAG scoped A",
            owner_id=tenant_a.subject,
            issuer=tenant_a.issuer,
            tenant_id=tenant_a.tenant_id,
        )
        document = rag.add_document(
            bot.id,
            "tenant-a-secret.txt",
            b"tenant-a-secret-needle",
        )
        assert [item["name"] for item in rag.list_documents(bot.id)] == [
            "tenant-a-secret.txt"
        ]
    finally:
        _reset_scope(tokens)

    tokens = _set_scope(tenant_b)
    try:
        assert rag.list_documents(bot.id) == []
        assert rag.search(bot.id, "tenant-a-secret-needle") == []
        assert rag.toggle_document(bot.id, document["id"], enabled=False) is False
        assert rag.delete_document(bot.id, document["id"]) is False
        with pytest.raises(PermissionError, match="当前认证范围"):
            rag.add_document(bot.id, "blocked.txt", b"blocked")
    finally:
        _reset_scope(tokens)

    tokens = _set_scope(tenant_a)
    try:
        restored = rag.get_document(bot.id, document["id"])
        assert restored is not None
        assert restored["enabled"] is True
    finally:
        _reset_scope(tokens)


def test_rag_legacy_import_is_deferred_until_the_owning_scope(tmp_path, monkeypatch):
    from core.agent import bot_manager as bot_module
    from core.agent.bot_manager import BotManager
    from core.agent.rag_manager import RagManager
    from core.auth import CurrentUser

    manager = BotManager(data_dir=str(tmp_path / "data"))
    monkeypatch.setattr(bot_module, "bot_manager", manager)
    rag = RagManager(data_root=str(tmp_path / "data" / "bots"))
    tenant_a = CurrentUser(
        "same-subject",
        claims={"iss": "https://issuer-a.example", "tenant_id": "tenant-a"},
    )
    tenant_b = CurrentUser(
        "same-subject",
        claims={"iss": "https://issuer-b.example", "tenant_id": "tenant-b"},
    )

    tokens = _set_scope(tenant_a)
    try:
        bot = manager.create_bot(
            "rag-legacy-scoped-a",
            "Legacy RAG scoped A",
            owner_id=tenant_a.subject,
            issuer=tenant_a.issuer,
            tenant_id=tenant_a.tenant_id,
        )
        knowledge_dir = Path(manager._bot_dir(bot.id)) / "knowledge"
        knowledge_dir.mkdir(parents=True, exist_ok=True)
        (knowledge_dir / "docs.json").write_text(
            json.dumps(
                [
                    {
                        "id": "legacy-doc",
                        "name": "legacy-secret.txt",
                        "stored_name": "legacy-doc_legacy-secret.txt",
                        "enabled": True,
                        "created_at": "2026-11-08 00:00:00",
                    }
                ]
            ),
            encoding="utf-8",
        )
        (knowledge_dir / "chunks.json").write_text(
            json.dumps(
                [
                    {
                        "chunk_id": "legacy-doc_c0",
                        "doc_id": "legacy-doc",
                        "doc_name": "legacy-secret.txt",
                        "index": 0,
                        "title": "secret",
                        "content": "tenant-a legacy secret",
                        "tokens": ["tenant-a", "legacy", "secret"],
                    }
                ]
            ),
            encoding="utf-8",
        )
    finally:
        _reset_scope(tokens)

    tokens = _set_scope(tenant_b)
    try:
        assert rag.list_documents(bot.id) == []
        assert (knowledge_dir / "docs.json").exists()
        assert (knowledge_dir / "chunks.json").exists()
    finally:
        _reset_scope(tokens)

    tokens = _set_scope(tenant_a)
    try:
        assert [item["name"] for item in rag.list_documents(bot.id)] == [
            "legacy-secret.txt"
        ]
        assert not (knowledge_dir / "docs.json").exists()
        assert not (knowledge_dir / "chunks.json").exists()
    finally:
        _reset_scope(tokens)


def test_concurrent_bot_creation_cannot_overwrite_global_id(tmp_path):
    from core.agent.bot_manager import BotIdConflict, BotManager

    manager = BotManager(data_dir=str(tmp_path / "data"))
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, str]] = []

    def create(issuer: str, tenant_id: str, owner_id: str) -> None:
        bot_id = "race-bot"
        observed_free = manager.get_bot(bot_id) is None
        barrier.wait()
        assert observed_free is True
        try:
            manager.create_bot(
                bot_id,
                f"Bot {tenant_id}",
                owner_id=owner_id,
                issuer=issuer,
                tenant_id=tenant_id,
            )
        except BotIdConflict:
            outcomes.append((tenant_id, "conflict"))
        else:
            outcomes.append((tenant_id, "created"))

    threads = [
        threading.Thread(
            target=create,
            args=("https://issuer-a.example", "tenant-a", "owner-a"),
        ),
        threading.Thread(
            target=create,
            args=("https://issuer-b.example", "tenant-b", "owner-b"),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(status for _tenant, status in outcomes) == ["conflict", "created"]
    final = manager.get_bot("race-bot")
    assert final is not None
    created_tenant = next(tenant for tenant, status in outcomes if status == "created")
    assert final.tenant_id == created_tenant
    assert final.name == f"Bot {created_tenant}"


def test_deleted_idempotent_task_cannot_be_recreated_or_started(tmp_path, monkeypatch):
    import core.task_runner as task_runner

    database = tmp_path / "tim.sqlite3"
    storage = tmp_path / "storage"
    monkeypatch.setattr(task_runner, "IDEMPOTENCY_DB_PATH", str(database))
    monkeypatch.setattr(task_runner, "STORAGE_DIR", str(storage))
    monkeypatch.setattr(task_runner, "RESULTS_DIR", str(storage / "batch_results"))

    first_runner = task_runner.TaskRunner()
    original = first_runner.create_task(
        "c2c_msg_withdraw",
        [{"id": "deleted"}],
        delay_sec=0,
        dry_run=True,
        owner_id="owner",
        idempotency_key="deleted-key",
        request_fingerprint="deleted-fingerprint",
    )
    assert first_runner._store().delete_batch_task(original.task_id, allow_active=True)

    replay_runner = task_runner.TaskRunner()
    replay = replay_runner.create_task(
        "c2c_msg_withdraw",
        [{"id": "deleted"}],
        delay_sec=0,
        dry_run=True,
        owner_id="owner",
        idempotency_key="deleted-key",
        request_fingerprint="deleted-fingerprint",
    )
    invoked: list[str] = []
    replay_runner._worker = lambda task: invoked.append(task.task_id)

    assert replay.status == "DELETED"
    assert replay_runner._store().get_batch_task(original.task_id) is None
    assert replay_runner.start_task(replay.task_id, recover_reserved=True) is False
    assert invoked == []


def test_bot_delete_stops_runs_from_all_users_and_scopes():
    from core.agent.run_manager import AgentRunManager

    class BlockingEngine:
        def run_agent_stream(self, *args, **kwargs):
            while not kwargs["stop_checker"]():
                time.sleep(0.01)
                yield {"event": "message", "data": {}}
            yield {"event": "done", "data": {"status": "stopped"}}

    manager = AgentRunManager()
    run = manager.start_run(
        "delete-me",
        "stop this",
        BlockingEngine(),
        object(),
        user_id="owner",
        issuer="issuer",
        tenant_id="tenant",
        is_admin=False,
        is_operator=False,
    )
    deadline = time.time() + 1
    while manager.get_active_run("delete-me", "owner", "issuer", "tenant") is None:
        assert time.time() < deadline
        time.sleep(0.01)

    assert manager.stop_all_runs("delete-me") == 1
    deadline = time.time() + 1
    while run.status == "running":
        assert time.time() < deadline
        time.sleep(0.01)
    assert manager.get_active_run("delete-me", "owner", "issuer", "tenant") is None


def test_persistent_task_cache_does_not_survive_sqlite_deletion(tmp_path, monkeypatch):
    import core.task_runner as task_runner

    database = tmp_path / "tim.sqlite3"
    storage = tmp_path / "storage"
    monkeypatch.setattr(task_runner, "IDEMPOTENCY_DB_PATH", str(database))
    monkeypatch.setattr(task_runner, "STORAGE_DIR", str(storage))
    monkeypatch.setattr(task_runner, "RESULTS_DIR", str(storage / "batch_results"))

    runner = task_runner.TaskRunner()
    task = runner.create_task(
        "c2c_msg_withdraw",
        [{"id": "cache"}],
        delay_sec=0,
        dry_run=True,
        owner_id="owner",
    )
    assert runner._store().delete_batch_task(task.task_id, allow_active=True)
    assert runner.get_task(task.task_id) is None


def test_file_upload_quota_is_atomic_across_processes(tmp_path):
    database = tmp_path / "tim.sqlite3"
    data_dir = tmp_path / "data"
    environment = {
        **os.environ,
        "PYTHONPATH": str(PROJECT_ROOT),
        "APP_ENV": "development",
        "AUTH_ENABLED": "false",
        "TIM_DATA_DIR": str(data_dir),
        "TIM_DATABASE_URL": f"sqlite:///{database}",
        "TIM_SQLITE_PATH": str(database),
    }
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from core.agent.bot_manager import BotManager; "
                "BotManager().create_bot('quota-bot', 'Quota', "
                "owner_id='local', visibility='private')"
            ),
        ],
        cwd=str(PROJECT_ROOT),
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    child_code = """
import io
import sys
from fastapi.testclient import TestClient
import apis.agent as agent
from app import app
agent.MAX_AGENT_FILES_PER_BOT = 1
name = sys.argv[1]
response = TestClient(app).post(
    '/api/agent/bots/quota-bot/upload',
    files={'file': (name, io.BytesIO(name.encode()), 'text/plain')},
)
print(response.status_code)
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", child_code, name],
            cwd=str(PROJECT_ROOT),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for name in ("a.txt", "b.txt")
    ]
    results = [process.communicate(timeout=20) for process in processes]
    assert all(process.returncode == 0 for process in processes), results
    assert sorted(int(stdout.strip().splitlines()[-1]) for stdout, _stderr in results) == [200, 413]

    connection = sqlite3.connect(database)
    try:
        count = connection.execute(
            """
            SELECT COUNT(*)
            FROM bot_files
            WHERE bot_id = 'quota-bot' AND deleted_at IS NULL
            """
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 1


def test_file_operations_write_scoped_audit_events(tmp_path, monkeypatch):
    import apis.agent as agent_api
    from core.agent.bot_manager import BotManager

    manager = BotManager(data_dir=str(tmp_path / "data"))
    bot = manager.create_bot(
        "file-audit",
        "File audit",
        owner_id="local",
        visibility="private",
    )
    monkeypatch.setattr(agent_api, "bot_manager", manager)
    client = TestClient(__import__("app").app)

    uploaded = client.post(
        f"/api/agent/bots/{bot.id}/upload",
        files={"file": ("report.txt", io.BytesIO(b"report"), "text/plain")},
    )
    assert uploaded.status_code == 200
    stored_name = uploaded.json()["file"]["saved_name"]

    exported = client.get(f"/api/agent/bots/{bot.id}/files-export")
    assert exported.status_code == 200
    assert client.delete(f"/api/agent/bots/{bot.id}/files/{stored_name}").status_code == 200

    events = manager.audit_store.list_recent(issuer="", tenant_id="", limit=50)
    observed = {(event["action"], event["outcome"]) for event in events}
    assert ("agent.file.upload", "SUCCESS") in observed
    assert ("agent.file.export", "SUCCESS") in observed
    assert ("agent.file.delete", "SUCCESS") in observed

    uploaded_again = client.post(
        f"/api/agent/bots/{bot.id}/upload",
        files={"file": ("orphan.txt", io.BytesIO(b"orphan"), "text/plain")},
    )
    assert uploaded_again.status_code == 200
    orphan_path = uploaded_again.json()["file"]["path"]
    old = time.time() - 2 * 24 * 60 * 60
    os.utime(orphan_path, (old, old))
    assert manager.cleanup_unreferenced_files(bot.id, user_id="local") == 1
    assert manager.list_bot_files(bot.id, user_id="local") == []


def test_oversized_idempotency_key_is_client_error():
    from app import app

    response = TestClient(app).post(
        "/api/batch/create",
        headers={"Idempotency-Key": "x" * 257},
        json={
            "api_id": "c2c_msg_withdraw",
            "items": [{"To_Account": "invalid-key-test"}],
            "delay_sec": 0,
            "dry_run": True,
        },
    )
    assert response.status_code == 422
