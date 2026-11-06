from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from contextlib import suppress
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.agent.engine import _install_sandbox_scope
from core.agent.tools import (
    CallAgentTool,
    CloudCallTool,
    CreateBotTool,
    DeleteMemoryTool,
    EnhancePromptTool,
    ListAgentsTool,
    ListSkillsTool,
    ReadFileTool,
    RecallMemoryTool,
    RunSkillTool,
    SaveMemoryTool,
    SaveSkillTool,
    SearchKnowledgeTool,
    _validate_skill_ast,
    build_sandbox_scope,
)
from core.agent.risk import is_high_risk_api, matching_confirmation_paths
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


def test_tool_instances_do_not_expose_credential_dependencies():
    """Tool objects may enter smolagents static_tools, so clients stay off-instance."""
    cloud = SimpleNamespace(config={"tc_secret_id": "redacted", "tc_secret_key": "redacted"})
    tim = TimClient(config={"sdkappid": 1, "secret_key": "redacted"})
    tool = CloudCallTool(cloud_client=cloud, tim_client=tim)

    assert not hasattr(tool, "cloud_client")
    assert not hasattr(tool, "tim_client")
    assert not hasattr(tool, "approval_requester")
    assert not hasattr(tool, "bot_manager")

    skill_tool = RunSkillTool("bot", object(), tim, cloud_client=cloud)
    assert not hasattr(skill_tool, "cloud_client")
    assert not hasattr(skill_tool, "tim_client")

    # Exercise the same static-tools path used by CodeAgent, rather than only
    # checking the Python object directly.
    from smolagents.local_python_executor import LocalPythonExecutor

    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools({"cloud_call": tool})
    with pytest.raises(Exception, match="cloud_client"):
        executor("cloud_call.cloud_client.config")


def test_local_executor_timeout_returns_without_waiting_for_finite_worker(monkeypatch):
    """The caller deadline is bounded, while the worker remains non-killable."""
    import time

    from core.agent import engine as engine_module
    from smolagents.local_python_executor import LocalPythonExecutor

    executor = LocalPythonExecutor(additional_authorized_imports=["time"], timeout_seconds=0.1)
    started = time.monotonic()
    with pytest.raises(Exception, match="exceeded"):
        executor("import time\ntime.sleep(0.25)")
    elapsed = time.monotonic() - started
    assert elapsed < 0.15
    # This is deliberately a cooperative patch: the underlying thread is
    # still not killable and production hard isolation remains a process gate.
    assert engine_module.MAX_SANDBOX_FOR_ITERATIONS == 100_000


def test_killable_executor_terminates_timed_out_code_and_preserves_state_boundary():
    """Main CodeAgent execution must not leave timed-out code running late."""
    import time

    from core.agent.engine import KillablePythonExecutor

    executor = KillablePythonExecutor(["time"], timeout_seconds=0.1)
    executor.send_tools({})
    started = time.monotonic()
    try:
        with pytest.raises(Exception, match="exceeded"):
            executor('import time\ntime.sleep(0.4)\nlate_marker = "mutated"')
        elapsed = time.monotonic() - started
        assert elapsed < 0.25
        assert executor._process is None
        time.sleep(0.45)
        assert "late_marker" not in executor.state

        # A timed-out child can be replaced for the next CodeAgent step.
        executor.send_tools({})
        result = executor("print(2 + 3)")
        assert result.logs.strip() == "5"
    finally:
        executor.close()


def test_killable_executor_routes_approval_to_parent_without_early_rejection():
    import time

    from core.agent.engine import KillablePythonExecutor

    seen: list[dict] = []
    executor = KillablePythonExecutor([], timeout_seconds=0.2)

    def approve(request):
        time.sleep(0.5)
        seen.append(request)
        return "approve"

    executor.additional_functions["ask_for_approval"] = executor.approval_requester(approve)
    executor.send_tools({})
    try:
        result = executor("print(ask_for_approval({'approval_id': 'approval-1'}))\nimport time\ntime.sleep(0.05)")
        assert result.logs.strip() == "approve"
        assert seen == [{"approval_id": "approval-1"}]
    finally:
        executor.close()


def test_killable_executor_forwards_nested_orchestration_events():
    from core.agent.engine import KillablePythonExecutor

    records: list[dict] = []
    executor = KillablePythonExecutor([], timeout_seconds=1)
    executor.additional_functions["record_event"] = executor.orchestration_recorder(records.append)
    executor.send_tools({})
    try:
        executor("record_event({'target_bot_id': 'bot-2'})")
        assert records == [{"target_bot_id": "bot-2"}]
    finally:
        executor.close()


def test_killable_executor_switches_to_spawn_when_called_from_multithreaded_host(monkeypatch):
    """Forking from an AgentRun worker must not inherit unrelated thread locks."""
    from core.agent.engine import KillablePythonExecutor

    executor = KillablePythonExecutor([], timeout_seconds=1)
    original_active_count = threading.active_count
    monkeypatch.setattr(threading, "active_count", lambda: 2)
    try:
        executor.send_tools({})
        assert executor._spawn_worker is True
        assert executor("print(2 + 3)").logs.strip() == "5"
    finally:
        monkeypatch.setattr(threading, "active_count", original_active_count)
        executor.close()


def test_killable_executor_uses_spawn_when_fork_is_unavailable(monkeypatch):
    """Windows-style start-method inventories remain runnable via spawn."""
    import multiprocessing

    from core.agent.engine import KillablePythonExecutor

    monkeypatch.setattr(multiprocessing, "get_all_start_methods", lambda: ["spawn"])
    executor = KillablePythonExecutor([], timeout_seconds=1)
    try:
        executor.send_tools({})
        assert executor._spawn_worker is True
        assert executor("print('spawn')").logs.strip() == "spawn"
    finally:
        executor.close()


def test_spawn_executor_dispatches_parent_capabilities_without_pickling_tools(monkeypatch):
    from core.agent.engine import KillablePythonExecutor

    executor = KillablePythonExecutor([], timeout_seconds=1)
    original_active_count = threading.active_count
    monkeypatch.setattr(threading, "active_count", lambda: 2)
    seen: list[dict] = []
    executor.additional_functions["ask_for_approval"] = executor.approval_requester(
        lambda request: seen.append(request) or "approve"
    )
    try:
        executor.send_tools({"echo": lambda value: {"value": value}})
        result = executor(
            "print(echo(7)['value'])\n"
            "print(ask_for_approval({'approval_id': 'spawn-1'}))"
        )
        assert result.logs.splitlines() == ["7", "approve"]
        assert seen == [{"approval_id": "spawn-1"}]
    finally:
        monkeypatch.setattr(threading, "active_count", original_active_count)
        executor.close()


@pytest.mark.skipif(os.name != "posix", reason="liveness probe uses POSIX process inspection")
@pytest.mark.parametrize("force_spawn", [False, True])
def test_executor_worker_does_not_survive_parent_death(tmp_path, force_spawn):
    """The liveness pipe/PDEATHSIG path must reap fork and spawn workers."""
    probe = tmp_path / "orphan_probe.py"
    pid_file = tmp_path / "worker.pid"
    probe.write_text(
        "import os, sys, threading\n"
        "from pathlib import Path\n"
        "from core.agent.engine import KillablePythonExecutor\n"
        "if sys.argv[2] == 'spawn':\n"
        "    threading.active_count = lambda: 2\n"
        "if __name__ == '__main__':\n"
        "    executor = KillablePythonExecutor([], timeout_seconds=None)\n"
        "    executor.send_tools({})\n"
        "    Path(sys.argv[1]).write_text(str(executor._process.pid))\n"
        "    os._exit(0)\n"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    completed = subprocess.run(
        [sys.executable, str(probe), str(pid_file), "spawn" if force_spawn else "fork"],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    deadline = time.monotonic() + 3
    worker_pid = None
    while time.monotonic() < deadline:
        if pid_file.exists():
            worker_pid = int(pid_file.read_text())
            proc_stat = Path(f"/proc/{worker_pid}/stat")
            try:
                if not proc_stat.exists() or proc_stat.read_text().split()[2] == "Z":
                    return
            except FileNotFoundError:
                return
        time.sleep(0.05)
    if worker_pid is not None:
        with suppress(ProcessLookupError):
            os.kill(worker_pid, 9)
    pytest.fail(f"executor worker survived parent death: pid={worker_pid}")


def test_production_executor_selection_fails_closed_without_nsjail(monkeypatch):
    import core.agent.engine as engine_module

    from core.agent.engine import _create_code_executor

    monkeypatch.setattr(engine_module.sys, "platform", "linux")
    monkeypatch.setattr(engine_module.os, "name", "posix")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("TIM_CODE_EXECUTOR_MODE", "fork")
    with pytest.raises(RuntimeError, match="TIM_CODE_EXECUTOR_MODE=nsjail"):
        _create_code_executor([])


def test_non_linux_production_uses_spawn_rpc_with_explicit_warning(monkeypatch, caplog):
    import core.agent.engine as engine_module

    monkeypatch.setattr(engine_module.sys, "platform", "darwin")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("TIM_CODE_EXECUTOR_MODE", "fork")
    executor = engine_module._create_code_executor([])
    try:
        assert isinstance(executor, engine_module.KillablePythonExecutor)
        assert executor.force_spawn is True
        executor.send_tools({})
        assert executor._spawn_worker is True
        assert executor("print('controlled')").logs.strip() == "controlled"
    finally:
        executor.close()
    assert "spawn/RPC" in caplog.text
    assert "OS 级隔离" in caplog.text


def test_spawn_mode_selects_controlled_executor(monkeypatch):
    import core.agent.engine as engine_module

    monkeypatch.setattr(engine_module.sys, "platform", "darwin")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("TIM_CODE_EXECUTOR_MODE", "spawn")
    executor = engine_module._create_code_executor([])
    try:
        assert isinstance(executor, engine_module.KillablePythonExecutor)
        assert executor.force_spawn is True
    finally:
        executor.close()


def test_non_linux_production_rejects_nsjail_mode(monkeypatch):
    import core.agent.engine as engine_module

    monkeypatch.setattr(engine_module.sys, "platform", "darwin")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("TIM_CODE_EXECUTOR_MODE", "nsjail")
    with pytest.raises(RuntimeError, match="只支持 Linux 部署"):
        engine_module._create_code_executor([])


def test_executor_mode_rejects_unknown_value(monkeypatch):
    from core.agent.engine import _create_code_executor

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("TIM_CODE_EXECUTOR_MODE", "docker")
    with pytest.raises(RuntimeError, match="fork 或 nsjail"):
        _create_code_executor([])


def test_nsjail_rpc_proxies_do_not_expose_transport_internals():
    from core.agent.nsjail_worker import RpcFunctionProxy, RpcToolProxy

    class FakeClient:
        def call(self, kind, name, args, kwargs):
            return {"kind": kind, "name": name, "args": args, "kwargs": kwargs}

    tool = RpcToolProxy(FakeClient(), "cloud_call")
    assert tool("live/Describe", {})["name"] == "cloud_call"
    with pytest.raises(AttributeError):
        _ = tool._client

    function = RpcFunctionProxy(FakeClient(), "save_file")
    assert function("report.csv")["kind"] == "function_call"
    with pytest.raises(AttributeError):
        _ = function._client


def test_agent_run_stop_invokes_process_executor_terminator():
    from core.agent.run_manager import AgentRun

    run = AgentRun("run-stop", "bot-stop", "stop test")
    terminated: list[bool] = []
    run.set_execution_terminator(lambda: terminated.append(True))
    run.request_stop()
    assert terminated == [True]
    assert run.status == "stopped"


def test_local_executor_bounds_for_loop_iterations(monkeypatch):
    from core.agent import engine as engine_module
    from smolagents.local_python_executor import LocalPythonExecutor

    monkeypatch.setattr(engine_module, "MAX_SANDBOX_FOR_ITERATIONS", 3)
    executor = LocalPythonExecutor(additional_authorized_imports=["itertools"], timeout_seconds=2)
    with pytest.raises(Exception, match="for-loop iterations"):
        executor("import itertools\nfor _ in itertools.count():\n    pass")


def test_all_stateful_tools_hide_manager_engine_and_recursion_state():
    """Static tools must expose capabilities only through their forward methods."""
    from smolagents.local_python_executor import LocalPythonExecutor

    manager = SimpleNamespace()
    engine = SimpleNamespace(
        cloud_client=SimpleNamespace(config={"tc_secret_key": "redacted-cloud"}),
        tim_client=SimpleNamespace(config={"secret_key": "redacted-tim"}),
    )
    tools = {
        "enhance_self_prompt": EnhancePromptTool("bot", manager),
        "save_memory": SaveMemoryTool("bot", manager),
        "recall_memory": RecallMemoryTool("bot", manager),
        "delete_memory": DeleteMemoryTool("bot", manager),
        "save_skill": SaveSkillTool("bot", manager),
        "list_skills": ListSkillsTool("bot", manager),
        "create_bot": CreateBotTool(manager),
        "list_agents": ListAgentsTool(manager),
        "call_agent": CallAgentTool("bot", manager, engine=engine, call_chain=["bot", "child"]),
        "read_file": ReadFileTool("bot", manager),
        "search_knowledge": SearchKnowledgeTool("bot", rag_mgr=SimpleNamespace()),
        "run_skill": RunSkillTool("bot", manager, TimClient(), cloud_client=engine.cloud_client),
    }

    for tool in tools.values():
        assert not hasattr(tool, "bot_manager")
    for name in ("enhance_self_prompt", "save_memory", "recall_memory", "delete_memory", "save_skill", "list_skills", "call_agent", "read_file", "search_knowledge", "run_skill"):
        assert not hasattr(tools[name], "bot_id")
    assert not hasattr(tools["call_agent"], "engine")
    assert not hasattr(tools["call_agent"], "call_chain")

    executor = LocalPythonExecutor(additional_authorized_imports=[])
    executor.send_tools(tools)
    probes = (
        ("call_agent.engine.cloud_client.config", "engine"),
        ("read_file.bot_manager.get_llm_config()", "bot_manager"),
        ("call_agent.call_chain.clear()", "call_chain"),
        ("call_agent.call_chain.append('forged')", "call_chain"),
    )
    for code, marker in probes:
        with pytest.raises(Exception, match=marker):
            executor(code)


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


def test_high_risk_confirmation_cannot_be_scoped_to_entire_service():
    """A service label must not approve every destructive action in it."""
    meta = SimpleNamespace(
        api_id="live_DeleteLiveDomain",
        command="DeleteLiveDomain",
        action="DeleteLiveDomain",
        name="DeleteLiveDomain",
        full_command="v4/live/DeleteLiveDomain",
        service_name="live",
    )
    identities = matching_confirmation_paths("DeleteLiveDomain", meta)
    assert "live" not in identities
    assert "live/deletelivedomain" in identities
    assert "v4/live/deletelivedomain" in identities


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


def test_unknown_destructive_action_cannot_use_approved_fallback():
    """An approval callback cannot authorize an unregistered destructive path."""
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
        approval_requester=lambda _request: "approve",
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



def test_linux_production_allows_only_explicit_unconfined_spawn(monkeypatch, caplog):
    import core.agent.engine as engine_module

    monkeypatch.setattr(engine_module.sys, "platform", "linux")
    monkeypatch.setattr(engine_module.os, "name", "posix")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("TIM_CODE_EXECUTOR_MODE", "fork")
    monkeypatch.setenv("ALLOW_UNCONFINED_CODE_EXECUTOR", "true")
    executor = engine_module._create_code_executor([])
    try:
        assert isinstance(executor, engine_module.KillablePythonExecutor)
        assert executor.force_spawn is True
    finally:
        executor.close()
    assert "不提供 nsjail" in caplog.text
