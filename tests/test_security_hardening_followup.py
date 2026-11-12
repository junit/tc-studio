import io
import multiprocessing
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError


def test_public_bot_does_not_inherit_quarantined_legacy_state(tmp_path):
    from core.agent.bot_manager import BotManager
    from core.runtime_context import runtime_user_id

    manager = BotManager(data_dir=str(tmp_path / "data"))
    bot = manager.create_bot("public-legacy", "Public legacy", visibility="public")
    legacy_dir = manager._bot_dir(bot.id)
    os.makedirs(os.path.join(legacy_dir, "uploads"), exist_ok=True)
    with open(os.path.join(legacy_dir, "history.json"), "w", encoding="utf-8") as stream:
        stream.write('[{"id":"legacy","role":"assistant","content":"private old data"}]')

    token = runtime_user_id.set("alice")
    try:
        assert manager.get_history(bot.id) == []
        manager.migrate_legacy_state(bot.id, "alice")
        assert manager.get_history(bot.id)[0]["content"] == "private old data"
    finally:
        runtime_user_id.reset(token)

    token = runtime_user_id.set("bob")
    try:
        assert manager.get_history(bot.id) == []
    finally:
        runtime_user_id.reset(token)


def test_legacy_state_migration_api_is_admin_only_and_requires_target(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from apis import agent as agent_api
    from app import app
    from core.agent.bot_manager import BotManager
    from core.auth import CurrentUser

    manager = BotManager(data_dir=str(tmp_path / "data"))
    bot = manager.create_bot("legacy-api", "Legacy API", visibility="public")
    legacy_dir = manager._bot_dir(bot.id)
    with open(os.path.join(legacy_dir, "history.json"), "w", encoding="utf-8") as stream:
        stream.write('[{"id":"legacy","role":"assistant","content":"admin-selected legacy state"}]')
    monkeypatch.setattr(agent_api, "bot_manager", manager)

    client = TestClient(app)
    response = client.post(
        f"/api/agent/bots/{bot.id}/legacy-state/migrate",
        json={"target_user_id": "alice"},
    )
    assert response.status_code == 200
    assert response.json()["target_user_id"] == "alice"
    migrated = manager.metadata_store.read_conversation(
        bot_id=bot.id,
        limit=10,
        issuer="",
        tenant_id="",
        owner_subject="alice",
    )
    assert migrated["history"][0]["content"] == "admin-selected legacy state"
    assert not os.path.exists(manager._history_path(bot.id, "alice"))

    missing_target = client.post(
        f"/api/agent/bots/{bot.id}/legacy-state/migrate",
        json={},
    )
    assert missing_target.status_code == 422

    blank_target = client.post(
        f"/api/agent/bots/{bot.id}/legacy-state/migrate",
        json={"target_user_id": " "},
    )
    assert blank_target.status_code == 422

    app.dependency_overrides[agent_api.current_user] = lambda: CurrentUser(
        "operator-only",
        frozenset({"operator"}),
    )
    try:
        non_admin = client.post(
            f"/api/agent/bots/{bot.id}/legacy-state/migrate",
            json={"target_user_id": "bob"},
        )
        assert non_admin.status_code == 403
    finally:
        app.dependency_overrides.pop(agent_api.current_user, None)


def test_public_bot_prompt_contains_only_current_users_personal_knowledge(tmp_path, monkeypatch):
    from core.agent.engine import AgentEngine
    from core.agent.tools import build_bot_tools
    from core.agent.bot_manager import BotManager
    from core.tim_client import TimClient
    from core.agent.rag_manager import RagManager
    from core.runtime_context import runtime_user_id
    import core.agent.bot_manager as bot_module
    import core.agent.rag_manager as rag_module

    manager = BotManager(data_dir=str(tmp_path / "data"))
    public_bot = manager.create_bot("public-kb", "Public KB", visibility="public")
    private_bot = manager.create_bot("private-kb", "Private KB", visibility="private", owner_id="alice")

    rag = RagManager(data_root=manager.bots_dir)
    monkeypatch.setattr(bot_module, "bot_manager", manager)
    monkeypatch.setattr(rag_module, "rag_manager", rag)
    token = runtime_user_id.set("alice")
    try:
        rag.add_document(public_bot.id, "alice-secret.md", b"alice personal secret")
        rag.add_document(private_bot.id, "internal-secret.md", b"private bot secret")
    finally:
        runtime_user_id.reset(token)

    public_tools = build_bot_tools(
        public_bot.id,
        manager,
        TimClient(),
        include_knowledge_tool=False,
    )
    private_tools = build_bot_tools(
        private_bot.id,
        manager,
        TimClient(),
    )
    assert all(tool.name != "search_knowledge" for tool in public_tools)
    assert any(tool.name == "search_knowledge" for tool in private_tools)

    engine = AgentEngine(manager)
    assert "alice-secret.md" not in engine.assemble_system_prompt(public_bot)
    assert "internal-secret.md" in engine.assemble_system_prompt(private_bot)
    token = runtime_user_id.set("alice")
    try:
        assert "alice-secret.md" in engine.assemble_system_prompt(public_bot)
        assert "internal-secret.md" not in engine.assemble_system_prompt(public_bot)
    finally:
        runtime_user_id.reset(token)


def test_public_bot_personal_knowledge_upload_is_allowed(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from apis import agent as agent_api
    from app import app
    from core.agent.bot_manager import BotManager
    from core.agent.rag_manager import RagManager
    import core.agent.bot_manager as bot_module

    manager = BotManager(data_dir=str(tmp_path / "data"))
    bot = manager.create_bot("public-upload", "Public upload", visibility="public")
    monkeypatch.setattr(bot_module, "bot_manager", manager)
    monkeypatch.setattr(agent_api, "rag_manager", RagManager(data_root=manager.bots_dir))
    with patch.object(agent_api, "bot_manager", manager):
        response = TestClient(app).post(
            f"/api/agent/bots/{bot.id}/knowledge/upload",
            files={"file": ("secret.txt", b"private content", "text/plain")},
        )
    assert response.status_code == 200


def test_batch_delay_rejects_nan_and_infinity():
    import app as app_module
    from core.task_runner import TaskRunner

    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            app_module.CreateBatchRequest(
                api_id="c2c_msg_withdraw",
                items=[{"id": "1"}],
                delay_sec=value,
            )
        with pytest.raises(ValueError):
            TaskRunner().create_task(
                "c2c_msg_withdraw",
                [{"id": "1"}],
                delay_sec=value,
                dry_run=True,
            )


def test_batch_idempotency_is_deduplicated_and_conflicts_are_rejected(tmp_path, monkeypatch):
    import core.task_runner as task_runner_module

    storage_dir = tmp_path / "storage"
    monkeypatch.setattr(task_runner_module, "STORAGE_DIR", str(storage_dir))
    monkeypatch.setattr(task_runner_module, "RESULTS_DIR", str(storage_dir / "batch_results"))
    monkeypatch.setattr(
        task_runner_module,
        "IDEMPOTENCY_DB_PATH",
        str(storage_dir / "batch_idempotency.sqlite3"),
    )

    first_runner = task_runner_module.TaskRunner()
    first = first_runner.create_task(
        "c2c_msg_withdraw",
        [{"id": "1"}],
        delay_sec=0,
        dry_run=True,
        owner_id="alice",
        idempotency_key="request-1",
        request_fingerprint="fingerprint-1",
    )
    replay = first_runner.create_task(
        "c2c_msg_withdraw",
        [{"id": "1"}],
        delay_sec=0,
        dry_run=True,
        owner_id="alice",
        idempotency_key="request-1",
        request_fingerprint="fingerprint-1",
    )
    assert replay.task_id == first.task_id
    assert len(first_runner.tasks) == 1

    second_runner = task_runner_module.TaskRunner()
    cross_worker_replay = second_runner.create_task(
        "c2c_msg_withdraw",
        [{"id": "1"}],
        delay_sec=0,
        dry_run=True,
        owner_id="alice",
        idempotency_key="request-1",
        request_fingerprint="fingerprint-1",
    )
    assert cross_worker_replay.task_id == first.task_id
    assert cross_worker_replay.replay_only is True
    assert second_runner.start_task(cross_worker_replay.task_id) is True
    assert second_runner.tasks == {}

    with pytest.raises(task_runner_module.IdempotencyConflict):
        second_runner.create_task(
            "c2c_msg_withdraw",
            [{"id": "2"}],
            delay_sec=0,
            dry_run=True,
            owner_id="alice",
            idempotency_key="request-1",
            request_fingerprint="fingerprint-2",
        )


def test_rag_rolls_back_file_and_indexes_when_chunk_index_write_fails(tmp_path, monkeypatch):
    from core.agent.rag_manager import RagManager

    manager = RagManager(data_root=str(tmp_path))
    original_replace = manager.metadata_store.replace_rag_document

    def fail_after_metadata_commit(**kwargs):
        original_replace(**kwargs)
        raise OSError("simulated metadata failure")

    monkeypatch.setattr(
        manager.metadata_store,
        "replace_rag_document",
        fail_after_metadata_commit,
    )
    with pytest.raises(OSError, match="simulated metadata failure"):
        manager.add_document("bot", "secret.txt", b"secret content")

    assert manager.list_documents("bot") == []
    assert manager.metadata_store.list_rag_chunks("bot") == []
    assert list((tmp_path / "bot" / "knowledge" / "files").iterdir()) == []


def _add_rag_document_in_child(data_root, document_number):
    from core.agent.rag_manager import RagManager

    RagManager(data_root=data_root).add_document(
        "multi-process-bot",
        f"doc-{document_number}.txt",
        f"document-{document_number}".encode("utf-8"),
    )


def test_rag_cross_process_writers_do_not_lose_updates(tmp_path):
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_add_rag_document_in_child,
            args=(str(tmp_path), index),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0

    from core.agent.rag_manager import RagManager

    docs = RagManager(data_root=str(tmp_path)).list_documents("multi-process-bot")
    assert {doc["name"] for doc in docs} == {"doc-0.txt", "doc-1.txt"}


def test_pdf_and_read_file_limits_are_enforced(tmp_path):
    from pypdf import PdfWriter

    from core.agent.file_parser import MAX_PDF_PAGES, parse_file_summary
    from core.agent.tools import ReadFileTool

    payload = io.BytesIO()
    writer = PdfWriter()
    for _ in range(MAX_PDF_PAGES + 1):
        writer.add_blank_page(width=72, height=72)
    writer.write(payload)
    pdf_path = tmp_path / "too-many.pdf"
    pdf_path.write_bytes(payload.getvalue())

    summary = parse_file_summary(str(pdf_path), pdf_path.name)
    assert summary["error"] == "pdf_page_limit"

    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "large.txt").write_text("x" * 1000, encoding="utf-8")
    tool = ReadFileTool("bot", SimpleNamespace(_uploads_dir=lambda _bot_id: str(uploads)))
    assert "max_chars" in tool.forward("large.txt", -1)
    assert len(tool.forward("large.txt", 10)) <= 10 + 100
