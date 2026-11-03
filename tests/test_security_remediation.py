from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.agent.engine import _install_sandbox_scope
from core.agent.tools import CloudCallTool, _validate_skill_ast, build_sandbox_scope
from core.agent.risk import is_high_risk_api
from core.runtime_context import runtime_is_operator
from core.tim_client import TimClient


def test_model_scope_does_not_contain_raw_clients():
    client = TimClient(config={"sdkappid": 1, "secret_key": "secret"})
    scope = build_sandbox_scope(
        CloudCallTool(),
        cloud_client=SimpleNamespace(config={"tc_secret_key": "secret"}),
        tim_client=client,
    )
    assert "cloud_client" not in scope
    assert "tim_client" not in scope
    assert callable(scope["cloud_call"])

    from smolagents.local_python_executor import LocalPythonExecutor

    agent = SimpleNamespace(python_executor=LocalPythonExecutor(additional_authorized_imports=[]), state={})
    _install_sandbox_scope(agent, {"tim_client": client, "cloud_call": lambda *_args: {"ok": True}})
    assert "tim_client" not in agent.state


@pytest.mark.parametrize(
    "code",
    [
        "import random\ndef execute():\n    return random._os.system('id')",
        "import re\ndef execute():\n    return re.enum.sys.modules['os'].system('id')",
        "def execute():\n    return __builtins__['__loader__'].load_module('os')",
    ],
)
def test_skill_escape_forms_are_rejected(code: str):
    with pytest.raises(ValueError, match="技能代码"):
        _validate_skill_ast(code)


def test_skill_rpc_does_not_unpickle_worker_messages(tmp_path):
    import multiprocessing

    from core.agent.tools import _rpc_recv

    marker = tmp_path / "pickle-executed"

    class Evil:
        def __reduce__(self):
            return (open, (str(marker), "w"))

    parent, child = multiprocessing.Pipe()
    try:
        child.send(Evil())
        with pytest.raises(Exception):
            _rpc_recv(parent)
        assert not marker.exists()
    finally:
        parent.close()
        child.close()


def test_high_risk_alias_is_approved_after_registry_resolution():
    class FakeCloudClient:
        def __init__(self):
            self.calls = []

        def invoke_api(self, meta, payload):
            self.calls.append(meta.command)
            return {"ActionStatus": "OK"}

    approvals = []
    fake = FakeCloudClient()
    tool = CloudCallTool(
        cloud_client=fake,
        approval_requester=lambda request: (approvals.append(request), "approve")[1],
    )
    token = runtime_is_operator.set(True)
    try:
        result = tool.forward("tim_1624", {"GroupId": "@TGS#test"})
    finally:
        runtime_is_operator.reset(token)
    assert result["ActionStatus"] == "OK"
    assert fake.calls == ["destroy_group"]
    assert approvals and approvals[0]["canonical_action"] == "destroy_group"


def test_workbench_high_risk_alias_requires_confirmation(monkeypatch):
    import app as app_module

    meta = SimpleNamespace(
        api_id="group_destroy",
        command="destroy_group",
        action="destroy_group",
        name="解散群组",
        full_command="v4/group_open_http_svc/destroy_group",
        service_name="group_open_http_svc",
        build_payload=lambda payload: payload,
    )
    monkeypatch.setattr(app_module.registry, "get_api", lambda _name: meta)
    req = app_module.ApiInvokeRequest(payload={}, dry_run=False)
    with pytest.raises(HTTPException) as exc_info:
        app_module.invoke_single_api("tim_1624", req, app_module.CurrentUser("op", frozenset({"operator"})))
    assert exc_info.value.status_code == 428


@pytest.mark.parametrize(
    "command",
    [
        "DeleteLiveDomain",
        "DeleteLiveTranscodeTemplate",
        "clear_group_msg",
        "group_msg_recall",
        "admin_msgwithdraw",
        "kick",
        "friend_delete_all",
        "destroy_official_account",
    ],
)
def test_destructive_catalog_commands_require_approval(command: str):
    meta = SimpleNamespace(
        api_id=f"live_{command}",
        command=command,
        action=command,
        name=command,
        service_name="live",
    )
    assert is_high_risk_api(meta.api_id, meta)


def test_unknown_destructive_action_fails_closed_for_agent_call():
    class FakeTc3:
        def __init__(self):
            self.calls = []

        def invoke(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {"ActionStatus": "OK"}

    class FakeCloud:
        def __init__(self):
            self.tc3_client = FakeTc3()

    cloud = FakeCloud()
    tool = CloudCallTool(
        cloud_client=cloud,
        approval_requester=None,
    )
    token = runtime_is_operator.set(True)
    try:
        result = tool.forward("DeleteFutureLiveDomain", {"DomainName": "victim.example"})
    finally:
        runtime_is_operator.reset(token)
    assert result["ActionStatus"] == "CANCELLED"
    assert cloud.tc3_client.calls == []


def test_batch_json_rejects_scalar_rows():
    from app import _parse_batch_upload

    rows, error = _parse_batch_upload("rows.json", b"[1, 2]")
    assert rows == []
    assert error and "对象" in error

