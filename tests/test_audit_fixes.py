"""
全维度代码审计与证伪防护单元测试套件
验证针对 P0/P1/P2 重大缺陷的防御性加固：
1. 沙箱执行白名单去除 "os"，封堵代码执行提权路径
2. RunSkillTool 静态 AST 审查与非阻塞超时机制
3. 环境配置多值更新时对 None 与空掩码的防损坏覆盖保护
4. safe_int 在 API 数据流装配时的空字符串健壮容错
5. TaskRunner 异常容灾与单项错误隔离保护
6. AgentRun 异常状态机终态保护（杜绝 error 状态被 done 覆盖）
7. 附件文件打包重名去重保护
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from apis.agent import _validate_model_base_url
from apis.base import safe_int
from apis.c2c_msg import BatchSendMsgApi, SendMsgApi
from apis.group import AddGroupMemberApi
from core.agent.constants import AUTHORIZED_IMPORTS
from core.agent.run_manager import AgentRun
from core.agent.tools import RunSkillTool
from core.config import load_raw_config, save_environment
from core.task_runner import TaskRunner
from core.tim_client import TimClient


def test_authorized_imports_sandbox_os_removed():
    """验证 Python 沙箱导入白名单中已彻底移除 'os' 模块"""
    assert "os" not in AUTHORIZED_IMPORTS
    assert "subprocess" not in AUTHORIZED_IMPORTS
    assert "sys" not in AUTHORIZED_IMPORTS
    assert "shutil" not in AUTHORIZED_IMPORTS




def _save_environment_in_child(config_path: str, index: int):
    import core.config as config_module

    config_module.CONFIG_PATH = config_path
    config_module.save_environment({"id": f"proc_env_{index}", "name": f"proc-{index}"})


def test_agent_sandbox_scope_survives_executor_send_tools(tmp_path):
    """扩展沙箱工具必须在 CodeAgent.run 的 send_tools 后仍可用。"""
    from types import SimpleNamespace

    from smolagents.local_python_executor import LocalPythonExecutor

    from core.agent.engine import _install_sandbox_scope

    def render_chart(*_args):
        return "chart"

    def save_file(*_args):
        return {"filename": "out.txt"}

    def confined_open(*_args, **_kwargs):
        return None

    output_dir = str(tmp_path / "uploads")
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    agent = SimpleNamespace(python_executor=executor, state={})
    _install_sandbox_scope(agent, {
        "render_chart": render_chart,
        "save_file": save_file,
        "open": confined_open,
        "OUTPUT_DIR": output_dir,
    })
    executor.send_tools({})

    assert executor.static_tools is not None
    assert executor.static_tools["render_chart"] is render_chart
    assert executor.static_tools["save_file"] is save_file
    assert executor.static_tools["open"] is confined_open
    assert agent.state["OUTPUT_DIR"] == output_dir




def test_agent_sandbox_scope_preserves_callable_client_methods():
    """可调用的客户端实例必须保留为对象，不能被 safer_func 降级。"""
    from types import SimpleNamespace

    from smolagents.local_python_executor import LocalPythonExecutor

    from core.agent.engine import _install_sandbox_scope

    client = TimClient(config={"sdkappid": 1, "secret_key": "secret", "admin_user": "admin"})
    executor = LocalPythonExecutor(additional_authorized_imports=[])
    agent = SimpleNamespace(python_executor=executor, state={})
    _install_sandbox_scope(agent, {"tim_client": client})
    executor.send_variables(agent.state)
    executor.send_tools({})

    assert agent.state["tim_client"] is client
    assert "tim_client" not in (executor.static_tools or {})
    assert executor("tim_client.get_usersig()").output


def test_run_skill_ast_security_gate(tmp_path):
    """验证 RunSkillTool 静态 AST 对危险模块与逃逸属性的严格拦截"""
    from core.agent.bot_manager import BotManager
    mgr = BotManager(data_dir=str(tmp_path / "data"))
    bot = mgr.create_bot("test_sec_bot", "安全测试机器人")

    # 1. 拦截 import os
    mgr.save_skill(bot.id, "hack_os", "尝试导入 os", "import os\nexecute = lambda: os.system('whoami')")
    runner = RunSkillTool(bot.id, mgr, TimClient())
    res1 = runner.forward("hack_os")
    assert res1["success"] is False
    assert "安全拦截" in res1["error"]
    assert "os" in res1["error"]

    # 2. 拦截 import subprocess
    mgr.save_skill(bot.id, "hack_subproc", "尝试导入 subprocess", "from subprocess import Popen\nexecute = lambda: 1")
    res2 = runner.forward("hack_subproc")
    assert res2["success"] is False
    assert "安全拦截" in res2["error"]
    assert "subprocess" in res2["error"]

    # 3. 拦截 __subclasses__ 逃逸
    mgr.save_skill(bot.id, "hack_escape", "尝试原型逃逸", "execute = lambda: ().__class__.__bases__[0].__subclasses__()")
    res3 = runner.forward("hack_escape")
    assert res3["success"] is False
    assert "安全拦截" in res3["error"]
    assert "__subclasses__" in res3["error"]




def test_local_ollama_url_defaults_and_custom_port():
    """本地 Ollama 省略默认端口与自定义端口都应可被安全规范化。"""
    assert _validate_model_base_url("http://localhost") == "http://localhost:11434"
    assert _validate_model_base_url("http://localhost:8080") == "http://localhost:8080"
    assert _validate_model_base_url("http://127.0.0.1") == "http://127.0.0.1:11434"

    with pytest.raises(HTTPException) as exc_info:
        _validate_model_base_url("http://localhost:6379")
    assert exc_info.value.status_code == 422
    assert "允许列表" in str(exc_info.value.detail)

def test_legacy_xls_has_actionable_upload_error():
    """合法/非法 .xls 均不应被误报为损坏的 xlsx 压缩包。"""
    from app import _parse_batch_upload

    rows, error = _parse_batch_upload("legacy.xls", b"not-a-zip")
    assert rows == []
    assert error == "不支持旧版 .xls 格式，请另存为 .xlsx 后重新上传"


def test_file_parser_rejects_legacy_xls_before_openpyxl(tmp_path):
    """会话附件解析也必须给旧版 XLS 明确提示。"""
    from core.agent.file_parser import parse_file_summary

    path = tmp_path / "legacy.xls"
    path.write_bytes(b"not-a-zip")
    result = parse_file_summary(str(path), path.name)
    assert "不支持旧版 .xls" in result["summary"]



def test_approval_timeout_is_distinguished_from_rejection():
    """审批超时应向前端发出 timeout，便于恢复快照展示正确语义。"""
    run = AgentRun("run-timeout", "bot-timeout", "test")
    decision = run.request_approval({"approval_id": "approval-timeout", "timeout_seconds": 0.01})
    assert decision == "timeout"
    assert run.pending_approval is None
    assert run.stream_status == "executing"
    assert run.events[-1]["event"] == "approval_resolved"
    assert run.events[-1]["data"]["decision"] == "timeout"


def test_invalid_approval_decision_is_rejected():
    """审批接口不得接受 approve/reject 之外的任意字符串。"""
    run = AgentRun("run-invalid", "bot-invalid", "test")
    run.request_approval({"approval_id": "approval-invalid", "timeout_seconds": 1})
    assert run.resolve_approval("approval-invalid", "maybe") is False


def test_run_skill_approval_wait_does_not_consume_timeout(tmp_path, monkeypatch):
    """审批等待超过技能 deadline 后仍应返回明确的用户取消结果。"""

    import core.agent.tools as tools_module
    from core.agent.bot_manager import BotManager

    mgr = BotManager(data_dir=str(tmp_path / "data"))
    bot = mgr.create_bot("test_approval_timeout", "审批超时测试")
    decisions = []

    def slow_cancel(request):
        decisions.append(request)
        time.sleep(1.8)
        return "cancel"

    runner = RunSkillTool(bot.id, mgr, TimClient(), approval_requester=slow_cancel)
    monkeypatch.setattr(tools_module, "_SKILL_TIMEOUT_SECONDS", 1.5)
    mgr.save_skill(
        bot.id,
        "cancel_after_slow_approval",
        "慢审批取消",
        "result = cloud_call('destroy_group', {'GroupId': 'group-test'})",
    )

    result = runner.forward("cancel_after_slow_approval")

    assert len(decisions) == 1
    assert result["success"] is True
    assert result["result"]["ActionStatus"] == "CANCELLED"


def test_run_skill_worker_blocks_host_capabilities(tmp_path):
    """技能 worker 只能使用受限能力，不能借 open/pathlib/reflection 访问宿主机。"""
    from core.agent.bot_manager import BotManager

    mgr = BotManager(data_dir=str(tmp_path / "data"))
    bot = mgr.create_bot("test_worker_boundary", "安全测试")
    runner = RunSkillTool(bot.id, mgr, TimClient())

    blocked_skills = {
        "open_host": "def execute():\n    return open('/etc/hosts').read()",
        "pathlib_host": "import pathlib\ndef execute():\n    return pathlib.Path('/etc/hosts').read_text()",
        "reflection": "def execute():\n    return getattr(object, '__subclasses__')()",
    }
    for name, code in blocked_skills.items():
        mgr.save_skill(bot.id, name, name, code)
        result = runner.forward(name)
        assert result["success"] is False
        assert "安全拦截" in result["error"]


def test_run_skill_worker_supports_async_and_killable_timeout(tmp_path, monkeypatch):
    """async skill 必须被 await，超时必须终止隔离进程。"""
    import core.agent.tools as tools_module
    from core.agent.bot_manager import BotManager

    mgr = BotManager(data_dir=str(tmp_path / "data"))
    bot = mgr.create_bot("test_worker_lifecycle", "生命周期测试")
    runner = RunSkillTool(bot.id, mgr, TimClient())

    mgr.save_skill(bot.id, "async_skill", "async", "async def execute():\n    return 'async-ok'")
    assert runner.forward("async_skill") == {"success": True, "result": "async-ok"}

    monkeypatch.setattr(tools_module, "_SKILL_TIMEOUT_SECONDS", 0.2)
    mgr.save_skill(bot.id, "timeout_skill", "timeout", "import time\ntime.sleep(30)")
    timeout_result = runner.forward("timeout_skill")
    assert timeout_result["success"] is False
    assert "超时" in timeout_result["error"]


def test_safe_int_robustness():
    """验证 safe_int 容错转换各类边界入参"""
    assert safe_int(123) == 123
    assert safe_int("456") == 456
    assert safe_int("  789  ") == 789
    assert safe_int("") == 0
    assert safe_int("", default=2) == 2
    assert safe_int(None) == 0
    assert safe_int(None, default=5) == 5
    assert safe_int("invalid_string", default=9) == 9
    assert safe_int("12.34") == 0
    assert safe_int("1e3") == 0
    assert safe_int(float("nan"), default=7) == 7
    assert safe_int(float("inf"), default=7) == 7


def test_api_build_payload_empty_strings():
    """验证 API build_payload 在处理包含空字符串的数字字段时不崩溃"""
    send_api = SendMsgApi()
    p1 = send_api.build_payload({
        "Text": "测试消息",
        "SyncOtherMachine": "",
        "From_Account": "user1",
        "To_Account": "user2",
        "MsgRandom": ""
    })
    assert p1["SyncOtherMachine"] == 1
    assert p1["From_Account"] == "user1"
    assert p1["To_Account"] == "user2"
    assert isinstance(p1["MsgRandom"], int)

    batch_api = BatchSendMsgApi()
    p2 = batch_api.build_payload({
        "Text": "批量测试",
        "SyncOtherMachine": "",
        "To_Account": "user1,user2",
        "MsgRandom": ""
    })
    assert p2["SyncOtherMachine"] == 2
    assert p2["To_Account"] == ["user1", "user2"]
    assert isinstance(p2["MsgRandom"], int)

    add_api = AddGroupMemberApi()
    p3 = add_api.build_payload({
        "GroupId": "@TGS#test",
        "Member_Accounts": "user1,user2",
        "Silence": ""
    })
    assert p3["GroupId"] == "@TGS#test"
    assert p3["Silence"] == 0
    assert len(p3["MemberList"]) == 2


def test_save_environment_preserves_secret_on_null_field():
    """验证 save_environment 在前端传入 None 时不将真实密钥覆盖为 'None' 字符串"""
    raw_before = load_raw_config()
    active_env_id = raw_before.get("active_env_id", "env_default")
    env = next((e for e in raw_before.get("environments", []) if e.get("id") == active_env_id), None)
    orig_secret = env.get("secret_key", "") if env else ""

    # 提交包含 null / None 的 secret_key
    save_environment({
        "id": active_env_id,
        "secret_key": None
    })
    raw_after = load_raw_config()
    raw_env_after = next((e for e in raw_after.get("environments", []) if e.get("id") == active_env_id), None)
    assert raw_env_after is not None
    assert raw_env_after["secret_key"] == orig_secret


def test_agent_run_error_status_not_masked_by_done():
    """验证 AgentRun 在发生异常时，派发 done 事件不会掩盖 error 终态"""
    run = AgentRun(
        run_id="run_test_err",
        bot_id="default_assistant",
        user_message="测试异常状态保持"
    )
    assert run.status == "running"

    # 触发 error 事件
    run.dispatch_event("error", {"error": "致命调用失败"})
    assert run.status == "error"
    assert run.stream_status == "error"

    # 后续派发 done 事件（带 status=error）
    run.dispatch_event("done", {"status": "error"})
    assert run.status == "error"
    assert run.stream_status == "error"
    assert run.error == "致命调用失败"


def test_task_runner_isolated_item_error():
    """验证 TaskRunner 在遇到单个异常行时，不崩溃整个任务，单行标错并顺利完成整体批处理"""
    runner = TaskRunner()
    # 模拟包含 3 个项目的任务，其中第 2 项数据故意损坏
    items = [
        {"Text": "正常消息1", "From_Account": "u1", "To_Account": "u2"},
        {"Text": "", "From_Account": "", "To_Account": ""},
        {"Text": "正常消息3", "From_Account": "u3", "To_Account": "u4"},
    ]
    task = runner.create_task("c2c_send_msg", items, delay_sec=0.01, dry_run=True)
    runner._worker(task)

    assert task.status == "COMPLETED"
    assert task.total == 3
    assert len(task.results) == 3


def test_tim_client_callable():
    """验证 TimClient 支持直接作为函数调用"""
    client = TimClient()
    # 模拟 invoke 不真正请求网络
    original_invoke = client.invoke
    try:
        client.invoke = lambda service_name, command, payload, timeout=15: {"action": f"{service_name}/{command}", "payload": payload, "timeout": timeout}
        res = client(api_path="group_open_http_svc/get_appid_group_list", payload={"Limit": 10})
        assert res["action"] == "/group_open_http_svc/get_appid_group_list"
        assert res["payload"]["Limit"] == 10
    finally:
        client.invoke = original_invoke


def test_skill_ioc_and_sandbox_hardening(tmp_path):
    """验证 RunSkillTool 依赖注入 (cloud_call, tim_client) 与 builtins 沙箱封锁"""
    from core.agent.bot_manager import BotManager
    mgr = BotManager(data_dir=str(tmp_path / "data"))
    bot = mgr.create_bot("test_ioc_bot", "IoC测试机器人")
    runner = RunSkillTool(bot.id, mgr, TimClient())

    # 1. 自动注入 tim_client
    mgr.save_skill(bot.id, "test_ioc_skill", "测试IoC注入", "def execute(tim_client):\n    return 'injected_' + str(type(tim_client).__name__)")
    res = runner.forward("test_ioc_skill")
    assert res["success"] is True
    assert res["result"] == "injected_TimClient"

    # 2. 封锁 eval / exec / compile
    mgr.save_skill(bot.id, "test_eval_block", "测试eval封锁", "execute = lambda: eval('1+1')")
    res_eval = runner.forward("test_eval_block")
    assert res_eval["success"] is False
    assert "安全拦截" in res_eval["error"]

    # 3. 封锁 builtins 导入
    mgr.save_skill(bot.id, "test_builtins_block", "测试builtins封锁", "import builtins\nexecute = lambda: 1")
    res_b = runner.forward("test_builtins_block")
    assert res_b["success"] is False
    assert "安全拦截" in res_b["error"]


def test_api_aliases_resolution():
    """验证 GetGroupInfoApi, AccountDeleteApi, AccountCheckApi 的别名兼容性"""
    from apis.account import AccountCheckApi, AccountDeleteApi
    from apis.group import GetGroupInfoApi

    # 1. GetGroupInfoApi 别名支持
    group_api = GetGroupInfoApi()
    p1 = group_api.build_payload({"group_id": "@TGS#123"})
    assert p1["GroupIdList"] == ["@TGS#123"]
    p2 = group_api.build_payload({"GroupId": "@TGS#456,@TGS#789"})
    assert p2["GroupIdList"] == ["@TGS#456", "@TGS#789"]

    # 2. AccountDeleteApi 别名支持
    del_api = AccountDeleteApi()
    pd1 = del_api.build_payload({"users": "user1,user2"})
    assert pd1["DeleteItem"] == [{"UserID": "user1"}, {"UserID": "user2"}]
    pd2 = del_api.build_payload({"user_ids": ["u3", "u4"]})
    assert pd2["DeleteItem"] == [{"UserID": "u3"}, {"UserID": "u4"}]

    # 3. AccountCheckApi 别名支持
    chk_api = AccountCheckApi()
    pc1 = chk_api.build_payload({"users": "check1,check2"})
    assert pc1["CheckItem"] == [{"UserID": "check1"}, {"UserID": "check2"}]
    with pytest.raises(ValueError):
        del_api.build_payload({"DeleteItem": []})
    with pytest.raises(ValueError):
        group_api.build_payload({"GroupIdList": []})


def test_zip_archive_name_dedup_avoids_preexisting_suffix(tmp_path):
    from apis.agent import _unique_archive_name

    used = {"report.txt", "report (1).txt"}
    assert _unique_archive_name("report.txt", used) == "report (2).txt"
    assert _unique_archive_name("report.txt", used) == "report (3).txt"


def test_save_environment_serializes_concurrent_writers(tmp_path, monkeypatch):
    """并发环境更新必须完整成功且不能丢失其他 writer 的环境。"""
    import core.config as config_module

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(config_path))
    config_module.save_config({
        "active_env_id": "env_default",
        "environments": [{"id": "env_default", "name": "default", "secret_key": "secret"}],
    })

    def save_one(index):
        config_module.save_environment({"id": f"env_{index}", "name": f"env-{index}"})

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(save_one, range(20)))

    raw = config_module.load_raw_config()
    ids = {env["id"] for env in raw["environments"]}
    assert {"env_default", *(f"env_{i}" for i in range(20))} <= ids




def test_save_environment_serializes_cross_process_writers(tmp_path, monkeypatch):
    """多进程 worker 同时更新环境时不能丢失更新。"""
    import multiprocessing

    import core.config as config_module

    if config_module.fcntl is None or "spawn" not in multiprocessing.get_all_start_methods():
        pytest.skip("需要 POSIX fcntl 和 spawn 进程模型")

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(config_path))
    config_module.save_config({
        "active_env_id": "env_default",
        "environments": [{"id": "env_default", "name": "default", "secret_key": "secret"}],
    })

    ctx = multiprocessing.get_context("spawn")
    processes = [
        ctx.Process(target=_save_environment_in_child, args=(str(config_path), index))
        for index in range(8)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(5)
        assert process.exitcode == 0

    raw = config_module.load_raw_config()
    ids = {env["id"] for env in raw["environments"]}
    assert {"env_default", *(f"proc_env_{i}" for i in range(8))} <= ids


def test_batch_task_pause_stop_finalizes():
    """暂停中的 worker 收到 stop 后必须退出并归档终态。"""
    runner = TaskRunner()
    task = runner.create_task(
        "c2c_send_msg",
        [{"Text": "x", "From_Account": "a", "To_Account": "b"}],
        delay_sec=0.1,
        dry_run=True,
    )
    assert runner.start_task(task.task_id) is True
    time.sleep(0.02)
    task.pause()
    task.stop()
    deadline = time.time() + 2
    while task.end_time is None and time.time() < deadline:
        time.sleep(0.01)
    assert task.status == "CANCELLED"
    assert task.end_time is not None
    assert [event["type"] for event in task.events][-1] == "complete"


def test_batch_task_unknown_api_has_single_error_terminal_event():
    runner = TaskRunner()
    task = runner.create_task("__missing_api__", [{}], dry_run=True)
    assert runner.start_task(task.task_id) is True
    deadline = time.time() + 2
    while task.end_time is None and time.time() < deadline:
        time.sleep(0.01)
    assert task.status == "FAILED"
    assert task.end_time is not None
    assert [event["type"] for event in task.events] == ["error"]


def test_agent_run_manual_stop_preserves_stopped_status():
    run = AgentRun("run_stop", "default_assistant", "stop")
    run.request_stop()
    assert run.status == "stopped"
    assert run.stream_status == "done"
    assert [event["event"] for event in run.events] == ["done"]


def test_atomic_save_config():
    """验证 save_config 原子持久化写入机制"""
    from core.config import CONFIG_PATH, load_raw_config, save_config
    orig_raw = load_raw_config()
    try:
        test_cfg = dict(orig_raw)
        test_cfg["active_env_id"] = orig_raw.get("active_env_id", "env_default")
        save_config(test_cfg)
        loaded = load_raw_config()
        assert loaded["active_env_id"] == test_cfg["active_env_id"]
        # 验证临时文件已被原子替换且不残留
        assert not os.path.exists(f"{CONFIG_PATH}.tmp")
    finally:
        save_config(orig_raw)



def test_config_transaction_is_reentrant(tmp_path, monkeypatch):
    """嵌套事务和 save_config 不得因文件锁重复获取而死锁。"""
    import core.config as config_module

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(config_path))
    initial = {
        "active_env_id": "env_default",
        "environments": [{"id": "env_default", "name": "default"}],
    }
    config_module.save_config(initial)

    with config_module._config_transaction():
        config_module.save_config(initial)
        with config_module._config_transaction():
            config_module.save_config(initial)

    assert config_module.load_raw_config()["active_env_id"] == "env_default"


def test_local_model_ports_can_be_explicitly_configured(monkeypatch):
    """第三方本地运行时端口只能通过显式白名单启用。"""
    with pytest.raises(HTTPException) as exc_info:
        _validate_model_base_url("http://localhost:1234")
    assert exc_info.value.status_code == 422
    monkeypatch.setenv("LOCAL_MODEL_ALLOWED_PORTS", "11434,1234")
    assert _validate_model_base_url("http://localhost:1234") == "http://localhost:1234"


def test_high_risk_rejection_exposes_required_path(tmp_path):
    """手动高危调试拒绝时返回可直接复制的 API 路径。"""
    from core.agent.bot_manager import BotManager

    mgr = BotManager(data_dir=str(tmp_path / "data"))
    bot = mgr.create_bot("test_manual_approval", "手动审批测试")
    mgr.save_skill(
        bot.id,
        "dangerous_skill",
        "高危技能",
        "result = cloud_call('destroy_group', {'GroupId': 'test'})",
    )
    from core.agent.tools import CloudCallTool
    runner = CloudCallTool(
        bot_manager=mgr,
        tim_client=TimClient(),
        approval_requester=lambda _approval: "reject",
    )
    result = runner.forward("group_open_http_svc/destroy_group", {"GroupId": "test"})
    assert result["RequiredApprovalPath"] == "group_open_http_svc/destroy_group"
    assert "approved_high_risk_paths" in result["ErrorInfo"]

