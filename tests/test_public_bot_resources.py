from pathlib import Path

from fastapi.testclient import TestClient

from core.agent.bot_manager import BotManager
from core.agent.engine import AgentEngine, _confined_open, _save_generated_file
from core.agent.generated_files import GeneratedFileSession
from core.agent.rag_manager import RagManager
from core.auth import CurrentUser
from core.runtime_context import runtime_auth_scope, runtime_user_id


def test_public_bot_personal_files_history_memory_and_rag_isolate_full_http_chain(tmp_path, monkeypatch):
    import app as app_module
    import apis.agent as routes
    import core.auth as auth
    import core.agent.bot_manager as bot_module
    import core.agent.rag_manager as rag_module

    manager = BotManager(data_dir=str(tmp_path / "data"))
    bot_id = "default_assistant"
    rag = RagManager(data_root=manager.bots_dir)
    monkeypatch.setattr(routes, "bot_manager", manager)
    monkeypatch.setattr(bot_module, "bot_manager", manager)
    monkeypatch.setattr(routes, "rag_manager", rag)
    monkeypatch.setattr(rag_module, "rag_manager", rag)
    alice = CurrentUser("alice", frozenset({"operator"}), claims={"iss": "https://issuer-a", "tenant_id": "tenant-a"})
    user = alice
    monkeypatch.setattr(auth, "authenticate_request", lambda _request: user)
    client = TestClient(app_module.app)

    def run_as(active_user, function):
        token = runtime_user_id.set(active_user.subject)
        scope_token = runtime_auth_scope.set(active_user.auth_scope)
        try:
            return function()
        finally:
            runtime_auth_scope.reset(scope_token)
            runtime_user_id.reset(token)

    def generate_personal_output():
        session = GeneratedFileSession(manager, bot_id, _save_generated_file, _confined_open)
        try:
            manager.append_history(bot_id, "user", "alice private conversation")
            manager.save_memory(bot_id, "preference", "alice private memory")
            return session.save_file("private.txt", "alice private output", publish=True)
        finally:
            session.close()

    artifact = run_as(alice, generate_personal_output)
    upload = client.post(f"/api/agent/bots/{bot_id}/upload", files={"file": ("input.txt", b"alice private input", "text/plain")})
    assert upload.status_code == 200
    upload_url = upload.json()["file"]["url"]
    assert len(client.get(f"/api/agent/bots/{bot_id}/files").json()["files"]) == 2
    assert client.get(artifact["download_url"]).content == b"alice private output"
    knowledge = client.post(f"/api/agent/bots/{bot_id}/knowledge/upload", files={"file": ("alice-secret.txt", b"alpha_unique_private_secret operational runbook", "text/plain")})
    assert knowledge.status_code == 200
    doc_id = knowledge.json()["doc"]["id"]
    assert "alice-secret.txt" in run_as(alice, lambda: AgentEngine(manager).assemble_system_prompt(manager.get_bot(bot_id)))
    assert client.get(f"/api/agent/bots/{bot_id}/knowledge/docs/{doc_id}/download").content.startswith(b"alpha_unique_private_secret")

    # All are legitimate users of the same global public BOT. A reused subject,
    # even 'local', must not defeat issuer/tenant or filesystem boundaries.
    others = [
        CurrentUser("bob", frozenset({"operator"}), claims={"iss": "https://issuer-a", "tenant_id": "tenant-a"}),
        CurrentUser("alice", frozenset({"operator"}), claims={"iss": "https://issuer-b", "tenant_id": "tenant-a"}),
        CurrentUser("alice", frozenset({"operator"}), claims={"iss": "https://issuer-a", "tenant_id": "tenant-b"}),
        CurrentUser("local", frozenset({"admin"}), claims={"iss": "https://issuer-a", "tenant_id": "tenant-a"}),
    ]
    for other in others:
        user = other
        assert client.get(f"/api/agent/bots/{bot_id}/files").json()["files"] == []
        assert client.get(artifact["download_url"]).status_code == 404
        assert client.get(upload_url).status_code == 404
        assert client.delete(f"/api/agent/bots/{bot_id}/files/{artifact['saved_name']}").status_code == 404
        assert run_as(user, lambda: manager.get_history(bot_id)) == []
        assert run_as(user, lambda: manager.list_memories(bot_id)) == []
        assert client.get(f"/api/agent/bots/{bot_id}/knowledge/docs").json()["docs"] == []
        assert client.post(f"/api/agent/bots/{bot_id}/knowledge/search", json={"query": "alpha_unique_private_secret"}).json()["results"] == []
        assert client.get(f"/api/agent/bots/{bot_id}/knowledge/docs/{doc_id}/download").status_code == 404
        assert client.put(f"/api/agent/bots/{bot_id}/knowledge/docs/{doc_id}", json={"enabled": False}).status_code == 404
        assert client.delete(f"/api/agent/bots/{bot_id}/knowledge/docs/{doc_id}").status_code == 404
        assert "alice-secret.txt" not in run_as(user, lambda: AgentEngine(manager).assemble_system_prompt(manager.get_bot(bot_id)))

    user = others[0]
    bob_doc = client.post(f"/api/agent/bots/{bot_id}/knowledge/upload", files={"file": ("bob-secret.txt", b"beta_unique_private_secret operational runbook", "text/plain")})
    assert bob_doc.status_code == 200
    bob_id = bob_doc.json()["doc"]["id"]
    alice_path = run_as(alice, lambda: rag.get_document_file_path(bot_id, doc_id))
    bob_path = run_as(user, lambda: rag.get_document_file_path(bot_id, bob_id))
    assert Path(alice_path).parent != Path(bob_path).parent
    user = alice
    assert client.post(f"/api/agent/bots/{bot_id}/knowledge/search", json={"query": "alpha_unique_private_secret"}).json()["results"]
    assert client.put(f"/api/agent/bots/{bot_id}/knowledge/docs/{doc_id}", json={"enabled": False}).status_code == 200
    assert client.post(f"/api/agent/bots/{bot_id}/knowledge/search", json={"query": "alpha_unique_private_secret"}).json()["results"] == []
    assert client.delete(f"/api/agent/bots/{bot_id}/knowledge/docs/{doc_id}").status_code == 200
    assert Path(bob_path).is_file()


def test_public_knowledge_does_not_auto_inherit_pre_auth_files(tmp_path, monkeypatch):
    import core.agent.bot_manager as bot_module

    manager = BotManager(data_dir=str(tmp_path / "data"))
    monkeypatch.setattr(bot_module, "bot_manager", manager)
    rag = RagManager(data_root=manager.bots_dir)
    rag.add_document("default_assistant", "local-secret.txt", b"quarantined pre-auth knowledge")
    local_dir = rag._get_kb_dir("default_assistant")
    token = runtime_user_id.set("alice")
    scope_token = runtime_auth_scope.set(CurrentUser("alice", claims={"iss": "https://issuer"}).auth_scope)
    try:
        assert rag.list_documents("default_assistant") == []
        assert rag.search("default_assistant", "quarantined") == []
        assert rag._get_kb_dir("default_assistant") != local_dir
    finally:
        runtime_auth_scope.reset(scope_token)
        runtime_user_id.reset(token)
