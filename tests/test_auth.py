import asyncio
import json
import os
import sys
import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app as app_module
from app import app
from core import auth
from core.auth import CurrentUser


def test_agent_tools_hide_ownerless_private_bots_for_non_admin():
    from core.agent.tools import CallAgentTool, ListAgentsTool
    from core.runtime_context import runtime_is_admin, runtime_user_id

    bots = [
        {"id": "public", "name": "Public", "visibility": "public", "owner_id": None},
        {"id": "legacy", "name": "Legacy", "visibility": "private", "owner_id": None},
        {"id": "alice", "name": "Alice", "visibility": "private", "owner_id": "alice"},
    ]
    manager = SimpleNamespace(
        list_bots=lambda user_id=None: bots,
        get_bot=lambda bot_id: SimpleNamespace(id=bot_id, name=bot_id, visibility="private", owner_id=None),
    )
    user_token = runtime_user_id.set("alice")
    admin_token = runtime_is_admin.set(False)
    try:
        listed = ListAgentsTool(cast(Any, manager)).forward()
        assert {item["id"] for item in listed} == {"public", "alice"}
        result = CallAgentTool(
            "alice",
            cast(Any, manager),
            engine=SimpleNamespace(execute_subtask=lambda **_: pytest.fail("must not dispatch")),
        ).forward("legacy", "inspect")
        assert "未能" in result
    finally:
        runtime_is_admin.reset(admin_token)
        runtime_user_id.reset(user_token)


def test_empty_filtered_history_does_not_initialize_global_demo(monkeypatch):
    def empty_history():
        return []

    monkeypatch.setattr(app_module, "load_history", empty_history)
    monkeypatch.setattr(
        app_module.os.path,
        "exists",
        lambda _path: pytest.fail("non-admin must not initialize demo history"),
    )
    result = app_module.get_history_list(CurrentUser("alice", frozenset({"operator"})))
    assert result == {"status": "success", "data": []}


def test_production_fails_closed_when_auth_disabled(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.delenv("ALLOW_INSECURE_LOCAL_AUTH", raising=False)
    auth.reload_auth_settings()
    response = TestClient(app).get("/api/services")
    assert response.status_code == 503


def test_unrecognized_environment_fails_closed_when_auth_disabled(monkeypatch):
    """未知/拼写错误的环境名（如 qa、preprod、prodcution）不得静默回到本地管理员模式。"""
    monkeypatch.setenv("APP_ENV", "qa")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.delenv("ALLOW_INSECURE_LOCAL_AUTH", raising=False)
    auth.reload_auth_settings()
    response = TestClient(app).get("/api/services")
    assert response.status_code == 503
    monkeypatch.setenv("APP_ENV", "development")
    auth.reload_auth_settings()


def test_dry_run_redacts_credentials_for_non_admin(monkeypatch):
    from types import SimpleNamespace

    api_meta = SimpleNamespace(
        service_name="im",
        service_type="im",
        command="v4/im_open_login_svc/xxx",
        build_payload=lambda p: dict(p),
    )
    preview = {
        "preview_url": "https://console.tim.qq.com/v4/x?sdkappid=1&identifier=admin&usersig=SECRETUSERSIG&random=2",
        "curl_command": "curl -X POST 'https://console.tim.qq.com/v4/x?usersig=SECRETUSERSIG' -d '{}'",
        "headers": {"Content-Type": "application/json"},
    }
    monkeypatch.setattr(app_module.registry, "get_api", lambda _api_id: api_meta)
    monkeypatch.setattr(app_module.UniversalCloudClient, "generate_preview", lambda self, meta, payload: preview)
    request = app_module.ApiInvokeRequest(payload={"k": "v"}, dry_run=True)

    operator_result = app_module.invoke_single_api(
        "api-1", request, CurrentUser("user-1", frozenset({"operator"}))
    )
    assert operator_result["credentials_redacted"] is True
    assert "SECRETUSERSIG" not in operator_result["preview_url"]
    assert "SECRETUSERSIG" not in operator_result["curl_command"]
    assert "<redacted>" in operator_result["preview_url"]

    admin_result = app_module.invoke_single_api(
        "api-1", request, CurrentUser("admin-1", frozenset({"admin"}))
    )
    assert admin_result["credentials_redacted"] is False
    assert "SECRETUSERSIG" in admin_result["preview_url"]


def test_dry_run_redacts_tc3_authorization_header_for_non_admin(monkeypatch):
    from types import SimpleNamespace

    api_meta = SimpleNamespace(
        service_name="cvm",
        service_type="cvm",
        command="DescribeInstances",
        build_payload=lambda p: dict(p),
    )
    preview = {
        "preview_url": "https://cvm.tencentcloudapi.com",
        "curl_command": 'curl -X POST "https://cvm.tencentcloudapi.com" \\\n  -H "Authorization: TC3-HMAC-SHA256 Credential=AKID..., SignedHeaders=..., Signature=SIG123" \\\n  -d "{}"',
        "headers": {"Authorization": "TC3-HMAC-SHA256 Credential=AKID..., Signature=SIG123", "Host": "cvm.tencentcloudapi.com"},
    }
    monkeypatch.setattr(app_module.registry, "get_api", lambda _api_id: api_meta)
    monkeypatch.setattr(app_module.UniversalCloudClient, "generate_preview", lambda self, meta, payload: preview)
    request = app_module.ApiInvokeRequest(payload={}, dry_run=True)

    result = app_module.invoke_single_api(
        "api-2", request, CurrentUser("user-1", frozenset({"operator"}))
    )
    assert result["credentials_redacted"] is True
    assert "SIG123" not in result["curl_command"]
    assert "<redacted>" in result["curl_command"]
    assert result["headers"]["Authorization"] == "<redacted>"
    assert result["headers"]["Host"] == "cvm.tencentcloudapi.com"


def test_regenerate_does_not_duplicate_user_message(monkeypatch):
    import apis.agent as agent_api

    captured = {}
    monkeypatch.setattr(agent_api.bot_manager, "get_bot", lambda _bot_id: object())
    monkeypatch.setattr(
        agent_api.bot_manager,
        "get_last_user_message",
        lambda _bot_id, user_id=None: {"content": "retry me", "metadata": {"attachments": []}},
    )
    monkeypatch.setattr(agent_api.bot_manager, "pop_last_assistant_message", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        agent_api.agent_run_manager,
        "start_run",
        lambda **kwargs: captured.update(kwargs) or object(),
    )
    monkeypatch.setattr(agent_api, "_stream_run_sse", lambda _run: "stream")

    result = asyncio.run(
        agent_api.regenerate_bot_stream("bot-1", CurrentUser("user-1", frozenset({"operator"})))
    )

    assert result == "stream"
    assert captured["skip_user_append"] is True


def test_auth_disabled_preserves_local_mode(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    auth.reload_auth_settings()
    response = TestClient(app).get("/api/services")
    assert response.status_code == 200


def test_oidc_mode_rejects_anonymous_api_request(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_PROVIDER", "oidc")
    auth.reload_auth_settings()
    response = TestClient(app).get("/api/services")
    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"
    monkeypatch.setenv("AUTH_ENABLED", "false")
    auth.reload_auth_settings()


def test_archived_task_access_falls_back_to_history(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    auth.reload_auth_settings()
    monkeypatch.setattr(app_module.task_runner, "get_task", lambda _task_id: None)
    monkeypatch.setattr(app_module, "load_history", lambda: [{"task_id": "old-1", "owner_id": "user-1"}])
    result = app_module.require_batch_task("old-1", CurrentUser("user-1", frozenset({"operator"})))
    assert isinstance(result, dict)
    assert result["task_id"] == "old-1"
    monkeypatch.setenv("AUTH_ENABLED", "false")
    auth.reload_auth_settings()


def test_archived_task_denies_wrong_owner(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    auth.reload_auth_settings()
    monkeypatch.setattr(app_module.task_runner, "get_task", lambda _task_id: None)
    monkeypatch.setattr(app_module, "load_history", lambda: [{"task_id": "old-2", "owner_id": "user-1"}])
    with pytest.raises(HTTPException):
        app_module.require_batch_task("old-2", CurrentUser("user-2", frozenset({"operator"})))
    monkeypatch.setenv("AUTH_ENABLED", "false")
    auth.reload_auth_settings()


def test_disabled_mode_grants_local_admin_for_sensitive_dependency(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    auth.reload_auth_settings()
    response = TestClient(app).get("/api/agent/config")
    assert response.status_code == 200


def test_public_bot_history_is_partitioned(tmp_path):
    from core.agent.bot_manager import BotManager
    from core.runtime_context import runtime_user_id

    temp_bot_manager = BotManager(data_dir=str(tmp_path / "data"))
    bot = temp_bot_manager.create_bot("public_bot", "公共助手", owner_id=None, visibility="public")
    first = runtime_user_id.set("alice")
    try:
        temp_bot_manager.append_history(bot.id, "user", "alice-secret")
        assert [item["content"] for item in temp_bot_manager.get_history(bot.id)] == ["alice-secret"]
    finally:
        runtime_user_id.reset(first)
    second = runtime_user_id.set("bob")
    try:
        assert temp_bot_manager.get_history(bot.id) == []
        temp_bot_manager.append_history(bot.id, "user", "bob-secret")
        assert temp_bot_manager.get_history(bot.id)[0]["content"] == "bob-secret"
    finally:
        runtime_user_id.reset(second)


def test_public_bot_legacy_state_is_migrated_per_user(tmp_path, monkeypatch):
    """Pre-auth public history/files remain visible after OIDC partitioning."""
    from apis import agent as agent_api
    from core.agent.bot_manager import BotManager
    from core.runtime_context import runtime_user_id

    manager = BotManager(data_dir=str(tmp_path / "data"))
    bot = manager.create_bot("legacy_public", "公共助手", owner_id=None, visibility="public")
    legacy_dir = manager._bot_dir(bot.id)
    os.makedirs(os.path.join(legacy_dir, "uploads"), exist_ok=True)
    legacy_message = {
        "id": "legacy-1",
        "role": "assistant",
        "content": "历史报告已保留",
        "metadata": {"attachments": [{"saved_name": "f_1_report.txt", "name": "report.txt"}]},
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    with open(os.path.join(legacy_dir, "history.json"), "w", encoding="utf-8") as stream:
        json.dump([legacy_message], stream)
    with open(os.path.join(legacy_dir, "memory.json"), "w", encoding="utf-8") as stream:
        json.dump([{"key": "legacy-fact", "content": "历史事实"}], stream)
    legacy_file = os.path.join(legacy_dir, "uploads", "f_1_report.txt")
    with open(legacy_file, "w", encoding="utf-8") as stream:
        stream.write("legacy report")

    monkeypatch_target = runtime_user_id.set("alice")
    try:
        assert manager.get_history(bot.id)[0]["content"] == "历史报告已保留"
        assert manager.list_memories(bot.id)[0].key == "legacy-fact"
        assert manager.list_bot_files(bot.id)[0]["name"] == "f_1_report.txt"
        partition_file = os.path.join(manager._uploads_dir(bot.id), "f_1_report.txt")
        assert os.path.isfile(partition_file)
        # The API file path resolves through the same migrated user partition.
        monkeypatch.setattr(agent_api, "bot_manager", manager)
        response = agent_api.get_bot_file(bot.id, "f_1_report.txt")
        assert response.path == partition_file
        assert manager.delete_bot_file(bot.id, "f_1_report.txt") is True
        assert manager.list_bot_files(bot.id) == []
    finally:
        runtime_user_id.reset(monkeypatch_target)

    bob_token = runtime_user_id.set("bob")
    try:
        assert manager.get_history(bot.id)[0]["id"] == "legacy-1"
        assert os.path.isfile(os.path.join(manager._uploads_dir(bot.id), "f_1_report.txt"))
    finally:
        runtime_user_id.reset(bob_token)


def test_auth_middleware_keeps_user_context_until_stream_finishes(monkeypatch):
    """The identity remains available while an ASGI streaming body is produced."""
    from core.runtime_context import runtime_user_id

    monkeypatch.setattr(auth, "authenticate_request", lambda _request: CurrentUser("stream-user"))
    sent = []

    async def endpoint(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await asyncio.sleep(0)
        await send({
            "type": "http.response.body",
            "body": runtime_user_id.get().encode(),
            "more_body": False,
        })

    middleware = auth.AuthenticationMiddleware(endpoint)

    async def run():
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await middleware(
            {"type": "http", "method": "GET", "path": "/api/stream", "raw_path": b"/api/stream",
             "query_string": b"", "headers": [], "scheme": "http", "server": ("test", 80),
             "client": ("test", 1), "http_version": "1.1", "asgi": {"version": "3.0"}},
            receive,
            send,
        )

    asyncio.run(run())
    assert sent[-1]["body"] == b"stream-user"
    assert runtime_user_id.get() == "local"


def test_jwks_network_fetch_is_not_serialized_by_cache_lock(monkeypatch):
    settings = SimpleNamespace(
        jwks_url="https://issuer.example.test/jwks",
        issuer="https://issuer.example.test",
        jwks_ttl=300,
    )
    started = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"keys": [{"kid": "key-1", "kty": "RSA"}]}

    def fake_get(*_args, **_kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
            call_number = calls
        if call_number == 1:
            started.set()
        else:
            second_entered.set()
        assert release.wait(timeout=2)
        return Response()

    monkeypatch.setattr(auth.requests, "get", fake_get)
    monkeypatch.setattr(auth, "_jwks_cache", None)
    results = []

    first = threading.Thread(target=lambda: results.append(auth._get_jwks(settings)))
    first.start()
    assert started.wait(timeout=1)
    second = threading.Thread(target=lambda: results.append(auth._get_jwks(settings)))
    second.start()
    assert second_entered.wait(timeout=1), "a slow JWKS request must not hold _jwks_lock"
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive() and not second.is_alive()
    assert len(results) == 2
