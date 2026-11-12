"""Policy, real spawn workers and HTTP governance tests for the catalog seam."""

from __future__ import annotations

import multiprocessing
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core.agent.bot_manager import BotManager
from core.agent.capabilities import (
    CAPABILITY_CATALOG, POLICY_KEY, CapabilityCatalog, CapabilityConflict,
    CapabilityContext, CapabilityDefinition, CapabilityPolicyError, CapabilitySession,
    describe_capabilities, load_capability_snapshot, update_capability_policy,
)
from core.agent.engine import AgentEngine
from core.agent.tools import CallAgentTool, RunSkillTool, _get_tool_dep, build_bot_tools
from core.auth import CurrentUser
from core.runtime_context import AuthScope, runtime_auth_scope, runtime_user_id
from core.tim_client import TimClient


@pytest.fixture
def manager(tmp_path):
    return BotManager(data_dir=str(tmp_path / "data"))


@contextmanager
def identity(issuer="", tenant="", subject="local"):
    token = runtime_auth_scope.set(AuthScope(issuer=issuer, tenant_id=tenant, subject=subject))
    user_token = runtime_user_id.set(subject)
    try:
        yield
    finally:
        runtime_user_id.reset(user_token)
        runtime_auth_scope.reset(token)


def disable(manager, *ids, snapshot=None):
    current = snapshot or load_capability_snapshot(manager)
    return update_capability_policy(manager, list(ids), expected_version=current.version,
                                    catalog_version=current.catalog_version)


@pytest.mark.parametrize("kind", ["agent", "delegate", "skill"])
def test_disabled_cloud_removes_named_tools_and_all_aliases(manager, kind):
    disable(manager, "cloud", "live_urls")
    with CapabilitySession(CapabilityContext("default_assistant", manager, TimClient(), kind=kind)) as session:
        assert "cloud_call" not in session.scope
        assert not any(key.endswith("_call") for key in session.scope)
        assert not any("cloud" in tool.name or tool.name.startswith("tim_") for tool in session.tools)
        assert "gen_live_push_url" not in session.scope
        assert "create_pdf_report" in session.scope


def test_disabled_files_disable_dependent_pdf_and_create_no_workdir(manager):
    disable(manager, "files")
    with CapabilitySession(CapabilityContext("default_assistant", manager, TimClient())) as session:
        assert not set(session.scope) & {"open", "save_file", "publish_file", "create_pdf_report", "OUTPUT_DIR", "WORKSPACE_DIR"}
        assert not session.files
        assert "read_file" not in [tool.name for tool in session.tools]
        assert not list(Path(session.uploads_dir).glob(".work-*"))
        assert not session.snapshot.enabled("pdf")


def test_snapshot_is_immutable_and_policy_changes_apply_only_to_new_tasks(manager):
    old = load_capability_snapshot(manager)
    with CapabilitySession(CapabilityContext("default_assistant", manager, TimClient(), snapshot=old)) as session:
        disable(manager, "files", "cloud", "delegation", "skills")
        assert callable(session.scope["create_pdf_report"])
        nested = [tool for tool in session.tools if isinstance(tool, (RunSkillTool, CallAgentTool))]
        assert len(nested) == 2
        assert all(_get_tool_dep(tool, "capability_snapshot") is old for tool in nested)
        runner = next(tool for tool in nested if isinstance(tool, RunSkillTool))
        manager.save_skill("default_assistant", name="snapshot_probe", description="probe", code="result = cloud_call('probe', {})")
        # No network: a host-owned capability records which snapshot was used.
        from core.agent.tools import CloudCallTool
        from unittest.mock import patch
        def synthetic_call(self, action_or_path: str, payload: dict, service: str | None = None) -> dict:
            return {"ok": True}
        with patch.object(CloudCallTool, "forward", new=synthetic_call):
            result = runner.forward("snapshot_probe")
        assert result["success"] and result["result"] == {"ok": True}
    new_tools = build_bot_tools("default_assistant", manager, TimClient())
    assert not {"run_skill", "call_agent", "cloud_call", "read_file"} & {t.name for t in new_tools}
    assert old.disabled_ids == frozenset()
    assert load_capability_snapshot(manager).disabled_ids == frozenset({"files", "cloud", "delegation", "skills"})


def _read_policy_in_other_process(database, conn):
    from core.persistence import SqliteAppSettingsStore
    store = SqliteAppSettingsStore(database)
    manager = SimpleNamespace(settings_store=store, database_path=database)
    conn.send(describe_capabilities(load_capability_snapshot(manager)))
    conn.recv()
    conn.send(describe_capabilities(load_capability_snapshot(manager)))
    conn.close()


def test_other_running_process_reads_new_sqlite_policy_without_restart(manager):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_read_policy_in_other_process, args=(manager.database_path, child))
    process.start()
    child.close()
    try:
        assert parent.poll(15)
        assert parent.recv()["version"] == 0
        disable(manager, "pdf")
        parent.send("reload")
        assert parent.poll(15)
        changed = parent.recv()
        assert changed["version"] == 1 and changed["disabled_ids"] == ["pdf"]
    finally:
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
    assert process.exitcode == 0


def test_scoped_policy_cas_conflicts_and_invalid_persisted_data_fail_closed(manager):
    old = load_capability_snapshot(manager)
    disable(manager, "pdf", snapshot=old)
    with pytest.raises(CapabilityConflict):
        disable(manager, "cloud", snapshot=old)
    with pytest.raises(CapabilityConflict):
        update_capability_policy(manager, [], expected_version=1, catalog_version="old-catalog")
    with pytest.raises(CapabilityPolicyError):
        disable(manager, "nonexistent")
    with pytest.raises(CapabilityPolicyError):
        disable(manager, "pdf", "pdf")
    with identity("issuer-a", "tenant-a", "alice"):
        assert load_capability_snapshot(manager).version == 0
        scoped = disable(manager, "cloud")
    with identity("issuer-b", "tenant-a", "alice"):
        assert load_capability_snapshot(manager).disabled_ids == frozenset()
        with pytest.raises(CapabilityPolicyError, match="认证范围"):
            scoped.validate_scope(manager)
    assert load_capability_snapshot(manager).disabled_ids == frozenset({"pdf"})
    manager.settings_store.set(POLICY_KEY, {"schema_version": 1, "disabled_ids": ["unknown"]})
    with pytest.raises(CapabilityPolicyError):
        load_capability_snapshot(manager)


@pytest.mark.parametrize("definition", [
    CapabilityDefinition("duplicate", "bad", "bad", exports=("same",)),
    CapabilityDefinition("reserved", "bad", "bad", exports=("cloud_client",)),
    CapabilityDefinition("invalid", "bad", "bad", exports=("__globals__",)),
    CapabilityDefinition("invalid-context", "bad", "bad"),
    CapabilityDefinition("builtin", "bad", "bad", exports=("getattr",)),
    CapabilityDefinition("keyword", "bad", "bad", exports=("class",)),
])
def test_catalog_rejects_collision_and_reserved_names(definition):
    catalog = CapabilityCatalog()
    catalog.register(CapabilityDefinition("first", "First", "first", exports=("same",)))
    with pytest.raises(ValueError):
        catalog.register(definition)


def test_catalog_rejects_dependency_cycle_and_freezes_after_use():
    catalog = CapabilityCatalog()
    catalog.register(CapabilityDefinition("first", "First", "first", requires=("second",)))
    catalog.register(CapabilityDefinition("second", "Second", "second", requires=("first",)))
    with pytest.raises(ValueError, match="循环"):
        catalog.definitions()
    catalog = CapabilityCatalog()
    catalog.register(CapabilityDefinition("first", "First", "first"))
    assert catalog.version
    with pytest.raises(ValueError, match="冻结"):
        catalog.register(CapabilityDefinition("second", "Second", "second"))


def test_reviewed_extension_binds_all_contexts_without_executor_changes(manager):
    catalog = CapabilityCatalog()
    catalog.register(CapabilityDefinition(
        "sample", "Sample", "trusted test extension", exports=("render_sample",),
        contexts=("agent", "delegate", "skill"), scope=lambda _c: {"render_sample": lambda x: {"sample": x}},
    ))
    snapshot = load_capability_snapshot(manager, catalog)
    for kind in ("agent", "delegate", "skill"):
        with CapabilitySession(CapabilityContext("default_assistant", manager, TimClient(), kind=kind, snapshot=snapshot)) as session:
            assert session.scope["render_sample"]("hi") == {"sample": "hi"}
            if kind != "skill":
                from core.agent.engine import KillablePythonExecutor, _install_sandbox_scope
                executor = KillablePythonExecutor([], force_spawn=True, timeout_seconds=3)
                try:
                    _install_sandbox_scope(SimpleNamespace(python_executor=executor, state={}), session.scope)
                    executor.send_tools({})
                    assert executor("print(render_sample('worker')['sample'])").logs.strip() == "worker"
                finally:
                    executor.close()
    from core.agent.tools import CloudCallTool
    runner = RunSkillTool("default_assistant", manager, TimClient(), capability_snapshot=snapshot)
    result = runner._run_isolated("result = render_sample('worker')", {}, CloudCallTool(), snapshot)
    assert result == {"success": True, "result": {"sample": "worker"}}


def test_real_skill_worker_generates_binary_files_and_pdf_and_cleans_scratch(manager):
    from pypdf import PdfReader
    manager.save_skill("default_assistant", name="report", description="report", code="""
def execute(save_file, create_pdf_report):
    with open('scratch.bin', 'wb') as stream:
        stream.write(b'\\x00\\xff')
    with open('scratch.bin', 'rb') as stream:
        sample = stream.read()
    save_file('sample.txt', sample, publish=True)
    pdf = create_pdf_report('report.pdf', '能力注册测试', '中文字体与二进制 RPC 回归测试。')
    return {'sample': sample, 'pdf': pdf}
""")
    result = RunSkillTool("default_assistant", manager, TimClient()).forward("report")
    assert result["success"], result
    assert result["result"]["sample"] == {"encoding": "base64", "data": "AP8="}
    files = result["files"]
    assert len(files) == 2
    assert Path(files[0]["path"]).read_bytes() == b"\x00\xff"
    reader = PdfReader(files[1]["path"])
    assert "中文字体" in "".join(page.extract_text() for page in reader.pages)
    assert not list(Path(manager._uploads_dir("default_assistant")).glob(".work-*"))


@pytest.mark.parametrize("code", [
    "result = cloud_call('probe', {})", "result = tim_call('probe', {})", "result = live_call('probe', {})",
    "result = cloud_client.invoke_api('probe', {})", "result = tim_client('probe', {})",
])
def test_skill_worker_cannot_use_disabled_cloud_aliases_or_legacy_clients(manager, code):
    manager.save_skill("default_assistant", name="disabled_probe", description="probe", code=code)
    disable(manager, "cloud")
    result = RunSkillTool("default_assistant", manager, TimClient()).forward("disabled_probe")
    assert result["success"] is False
    assert "not defined" in result["error"]


def test_standalone_skill_cannot_run_when_skills_disabled_and_rejects_array_args(manager):
    manager.save_skill("default_assistant", name="probe", description="probe", code="result = 1")
    runner = RunSkillTool("default_assistant", manager, TimClient())
    assert not runner.forward("probe", [1, 2])["success"]
    assert not runner.forward("probe", "[1, 2]")["success"]
    assert not runner.forward("probe", "invalid-json")["success"]
    disable(manager, "skills")
    assert runner.forward("probe")["error"] == "技能库能力已禁用"


def test_stream_and_delegate_cleanup_when_agent_constructor_fails(manager, monkeypatch):
    import core.agent.engine as engine_module
    engine = AgentEngine(manager)
    monkeypatch.setattr(engine, "create_model", lambda _cfg: object())
    closed = []
    original_factory = engine_module._create_code_executor

    def factory(*args, **kwargs):
        executor = original_factory(*args, **kwargs)
        original_close = executor.close
        def close():
            original_close()
            closed.append(executor)
        executor.close = close
        return executor

    def fail(**_kwargs):
        raise RuntimeError("synthetic constructor failure")

    monkeypatch.setattr(engine_module, "_create_code_executor", factory)
    monkeypatch.setattr(engine_module, "CodeAgent", fail)
    events = list(engine.run_agent_stream("default_assistant", "probe"))
    assert events[-1]["event"] == "error"
    assert "synthetic constructor" in events[-1]["data"]["error"]
    with pytest.raises(RuntimeError, match="synthetic constructor"):
        engine.execute_subtask("default_assistant", "css_live_operator", "probe")
    assert len(closed) == 2
    assert not list(Path(manager.bots_dir).rglob(".work-*"))


def test_policy_http_admin_only_strict_validation_conflict_and_tenant_isolation(manager, monkeypatch):
    import app as app_module
    import apis.agent as routes
    import core.auth as auth
    user = CurrentUser("reader", frozenset({"operator"}), claims={"iss": "issuer-a", "tenant_id": "tenant-a"})
    monkeypatch.setattr(routes, "bot_manager", manager)
    monkeypatch.setattr(auth, "authenticate_request", lambda _request: user)
    client = TestClient(app_module.app)
    assert client.get("/api/agent/capabilities").status_code == 403
    assert client.put("/api/agent/capabilities", json={"expected_version": 0, "catalog_version": CAPABILITY_CATALOG.version, "disabled_ids": []}).status_code == 403
    user = CurrentUser("admin", frozenset({"admin"}), claims={"iss": "issuer-a", "tenant_id": "tenant-a"})
    first = client.get("/api/agent/capabilities").json()
    payload = {"expected_version": first["version"], "catalog_version": first["catalog_version"], "disabled_ids": ["cloud"]}
    assert client.put("/api/agent/capabilities", json={**payload, "python_module": "os"}).status_code == 422
    assert client.put("/api/agent/capabilities", json={**payload, "expected_version": True}).status_code == 422
    changed = client.put("/api/agent/capabilities", json=payload)
    assert changed.status_code == 200
    assert changed.json()["disabled_ids"] == ["cloud"]
    assert client.put("/api/agent/capabilities", json=payload).status_code == 409
    user = CurrentUser("admin", frozenset({"admin"}), claims={"iss": "issuer-b", "tenant_id": "tenant-a"})
    assert client.get("/api/agent/capabilities").json()["disabled_ids"] == []
    assert load_capability_snapshot(manager).disabled_ids == frozenset()


def test_dependency_upgrade_cannot_silently_widen_imports(monkeypatch):
    import smolagents.local_python_executor as local
    from core.agent.sandbox_policy import AGENT_IMPORTS, create_local_evaluator
    monkeypatch.setattr(local, "BASE_BUILTIN_MODULES", [*local.BASE_BUILTIN_MODULES, "os"])
    evaluator = create_local_evaluator(AGENT_IMPORTS)
    assert set(evaluator.authorized_imports) == set(AGENT_IMPORTS)
    with pytest.raises(Exception, match="not allowed"):
        evaluator("import os")
    baseline = create_local_evaluator([])
    assert "time" in baseline.authorized_imports and "os" not in baseline.authorized_imports


def test_registry_rejects_live_dependency_objects_in_constant_scope(manager):
    catalog = CapabilityCatalog()
    catalog.register(CapabilityDefinition(
        "bad", "Bad", "host object must not enter sandbox", exports=("client",),
        scope=lambda c: {"client": c.manager},
    ))
    snapshot = load_capability_snapshot(manager, catalog)
    with pytest.raises(ValueError):
        CapabilitySession(CapabilityContext("default_assistant", manager, TimClient(), snapshot=snapshot))


def test_skill_artifacts_share_main_run_session_snapshot_and_history(manager, monkeypatch):
    import core.agent.engine as engine_module
    from core.agent.memory import memory_manager
    manager.save_skill("default_assistant", name="shared_report", description="report", code="""
def execute(create_pdf_report):
    return create_pdf_report('shared.pdf', '共享产物会话', '技能生成的文件进入当前会话历史。')
""")
    engine = AgentEngine(manager)
    monkeypatch.setattr(engine, "create_model", lambda _cfg: object())
    monkeypatch.setattr(memory_manager, "on_turn_completed", lambda **_kwargs: None)

    class ReplayAgent:
        def __init__(self, **kwargs):
            self.python_executor = kwargs["executor"]
            self.state = {}
            self.memory = SimpleNamespace(steps=[])
            self.tools = {tool.name: tool for tool in kwargs["tools"]}

        def run(self, _prompt, stream=True):
            disable(manager, "files", "skills")
            result = self.tools["run_skill"].forward("shared_report")
            assert result["success"], result
            yield engine_module.FinalAnswerStep("报告已生成")

    monkeypatch.setattr(engine_module, "CodeAgent", ReplayAgent)
    events = list(engine.run_agent_stream("default_assistant", "生成报告"))
    assert not [event for event in events if event["event"] == "error"]
    done = next(event["data"] for event in events if event["event"] == "done")
    assert len(done["files"]) == 1 and Path(done["files"][0]["path"]).is_file()
    assert manager.get_history("default_assistant")[-1]["metadata"]["artifacts"] == done["files"]
    assert not list(Path(manager._uploads_dir("default_assistant")).glob(".work-*"))


def test_call_agent_passes_snapshot_through_actual_delegated_setup(manager, monkeypatch):
    import core.agent.engine as engine_module
    engine = AgentEngine(manager)
    monkeypatch.setattr(engine, "create_model", lambda _cfg: object())
    observed = []

    class ReplayAgent:
        def __init__(self, **kwargs):
            self.python_executor = kwargs["executor"]
            self.state = {}
            observed.append({tool.name for tool in kwargs["tools"]})

        def run(self, _prompt):
            assert "create_pdf_report" in self.python_executor.additional_functions
            return "delegated-snapshot-ok"

    monkeypatch.setattr(engine_module, "CodeAgent", ReplayAgent)
    old = load_capability_snapshot(manager)
    with CapabilitySession(CapabilityContext("default_assistant", manager, TimClient(), engine=engine, snapshot=old)) as session:
        tool = next(tool for tool in session.tools if tool.name == "call_agent")
        disable(manager, "files", "delegation")
        result = tool.forward("css_live_operator", "probe")
    assert "delegated-snapshot-ok" in result
    assert len(observed) == 1 and "read_file" in observed[0]
    assert not list(Path(manager.bots_dir).rglob(".work-*"))


def test_disabled_capabilities_cannot_resurrect_from_previous_sandbox_state(manager, monkeypatch):
    import core.agent.engine as engine_module
    from core.agent.memory import memory_manager
    engine = AgentEngine(manager)
    monkeypatch.setattr(engine, "create_model", lambda _cfg: object())
    monkeypatch.setattr(memory_manager, "on_turn_completed", lambda **_kwargs: None)
    engine._remember_sandbox(engine._sandbox_key("default_assistant"), {
        "save_file": lambda *_a: "stale-file-function",
        "live_call": lambda *_a: "stale-cloud-function",
        "OUTPUT_DIR": "/stale/workspace",
        "user_data": {"count": 7},
    })
    disable(manager, "cloud", "files")

    class ReplayAgent:
        def __init__(self, **kwargs):
            self.python_executor = kwargs["executor"]
            self.state = {}
            self.memory = SimpleNamespace(steps=[])

        def run(self, _prompt, stream=True):
            assert self.state["user_data"] == {"count": 7}
            assert not {"save_file", "live_call", "OUTPUT_DIR"} & set(self.state)
            assert "save_file" not in self.python_executor.additional_functions
            yield engine_module.FinalAnswerStep("state-policy-ok")

    monkeypatch.setattr(engine_module, "CodeAgent", ReplayAgent)
    events = list(engine.run_agent_stream("default_assistant", "probe"))
    assert not [event for event in events if event["event"] == "error"]
    assert events[-1]["event"] == "done"
