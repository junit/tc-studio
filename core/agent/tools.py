"""
智能体工具集定义：TIM 166 API 工具、提示词自我强化、记忆持久化、技能自沉淀与执行
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import multiprocessing
import os
import re
import time
from collections.abc import Coroutine
from contextlib import suppress
from typing import Any, cast

from smolagents import Tool

from ..tim_client import TimClient
from .bot_manager import BotManager
from .constants import HIGH_RISK_API_KEYWORDS


class CloudCallTool(Tool):
    name = "cloud_call"
    description = (
        "统一调用腾讯云产品接口（涵盖云直播 API 3.0、即时通信 IM REST 及任意平台已注册的多云产品接口）。"
        "直接传入 Action 名称（如 'CreateCommonMixStream', 'DescribeLiveStreamState', 'DropLiveStream'）"
        "或 IM 服务路径（如 'openim/sendmsg', 'group_open_http_svc/get_group_info'），"
        "并传入入参 Payload JSON 字典，返回官方接口的结构化响应。"
    )
    inputs = {
        "action_or_path": {
            "type": "string",
            "description": "接口 Action 名称或服务路径，例如 'CreateCommonMixStream', 'DescribeLiveStreamState', 'DropLiveStream', 或 'openim/sendmsg'"
        },
        "payload": {
            "type": "object",
            "description": "请求参数 JSON 字典，遵循腾讯云官方 API 规范"
        },
        "service": {
            "type": "string",
            "description": "可选。云服务标识，如 'live' (云直播), 'im' (即时通信)。若缺省则由系统智能推导",
            "nullable": True
        }
    }
    output_type = "object"

    def __init__(
        self,
        approval_requester: Any | None = None,
        bot_manager: BotManager | None = None,
        tim_client: TimClient | None = None,
        cloud_client: Any | None = None,
        approval_wait_callback: Any | None = None
    ):
        super().__init__()
        self.approval_requester = approval_requester
        self.approval_wait_callback = approval_wait_callback
        self.bot_manager = bot_manager
        self.tim_client = tim_client
        self.cloud_client = cloud_client

    def forward(self, action_or_path: str, payload: Any, service: str | None = None) -> dict:
        target_name = (action_or_path or "").strip()

        raw_payload = payload
        if isinstance(raw_payload, str):
            try:
                parsed_payload = json.loads(raw_payload.strip())
                raw_payload = parsed_payload
            except Exception:
                try:
                    parsed_payload = ast.literal_eval(raw_payload.strip())
                    raw_payload = parsed_payload
                except Exception:
                    raw_payload = {}
        clean_payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}

        high_risk_kws = self.bot_manager.get_high_risk_keywords() if self.bot_manager else HIGH_RISK_API_KEYWORDS
        is_high_risk = any(k in target_name.lower() for k in high_risk_kws)

        if is_high_risk:
            if not self.approval_requester:
                return {
                    "ActionStatus": "CANCELLED",
                    "ErrorCode": -998,
                    "ErrorInfo": "高危操作缺少审批上下文，已按 fail-closed 策略拒绝执行。",
                }
            target_desc = []
            if isinstance(clean_payload, dict):
                # Live 云直播破坏性操作标识提取
                if "StreamName" in clean_payload:
                    target_desc.append(f"直播流: {clean_payload['StreamName']}")
                if "DomainName" in clean_payload:
                    target_desc.append(f"域名: {clean_payload['DomainName']}")
                if "AppName" in clean_payload:
                    target_desc.append(f"应用名: {clean_payload['AppName']}")
                if "ResumeTime" in clean_payload:
                    target_desc.append(f"解禁时间: {clean_payload['ResumeTime']}")
                # IM 即时通信破坏性操作标识提取
                if "GroupId" in clean_payload:
                    target_desc.append(f"群组 ID: {clean_payload['GroupId']}")
                if "GroupIdList" in clean_payload:
                    target_desc.append(f"群组列表: {len(clean_payload['GroupIdList'])} 个群")
                if "Accounts" in clean_payload:
                    target_desc.append(f"目标账号: {clean_payload['Accounts']}")
                if "UserID" in clean_payload:
                    target_desc.append(f"用户 ID: {clean_payload['UserID']}")
                if "Account" in clean_payload:
                    target_desc.append(f"账号: {clean_payload['Account']}")

            summary_str = "，".join(target_desc) if target_desc else "未指定具体目标标识"
            approval_id = f"appr_{int(time.time() * 1000)}"

            # 触发人机协同门禁挂起并等待授权
            approval_callback = self.approval_wait_callback
            if approval_callback:
                with suppress(Exception):
                    approval_callback(True)
            try:
                decision = self.approval_requester({
                    "approval_id": approval_id,
                    "api_path": target_name,
                    "payload": clean_payload,
                    "warning": f"检测到不可逆破坏性操作：{target_name}",
                    "target_summary": summary_str,
                    "timeout_seconds": 300
                })
            finally:
                if approval_callback:
                    with suppress(Exception):
                        approval_callback(False)

            if decision != "approve":
                return {
                    "ActionStatus": "CANCELLED",
                    "ErrorCode": -999,
                    "RequiredApprovalPath": target_name,
                    "ErrorInfo": (
                        f"⚠️ 操作已安全取消：用户拒绝或未获得「{target_name}」高危调用的执行授权。"
                        f"手动调试请同时设置 confirm_high_risk=true，"
                        f"并将 approved_high_risk_paths 设为包含该 API 路径。"
                    ),
                }

        try:
            from ..cloud_client import UniversalCloudClient
            from ..registry import registry

            api_meta = registry.get_api(target_name)
            cloud_client = self.cloud_client or UniversalCloudClient()

            if api_meta:
                res = cloud_client.invoke_api(api_meta, clean_payload)
            else:
                # 智能兜底推断
                svc = (service or "").lower().strip()
                if svc == "live" or target_name.startswith("live_") or (len(target_name) > 0 and target_name[0].isupper()):
                    res = cloud_client.tc3_client.invoke(
                        service="live",
                        action=target_name.replace("live_", ""),
                        version="2018-08-01",
                        payload=clean_payload
                    )
                else:
                    if self.tim_client:
                        res = self.tim_client.invoke("", target_name, clean_payload)
                    else:
                        res = cloud_client.tim_client.invoke("", target_name, clean_payload)

            if is_high_risk and isinstance(res, dict):
                res["_security_notice"] = {
                    "is_high_risk": True,
                    "user_approved": bool(self.approval_requester),
                    "warning": f"⚠️ 警告: 接口 '{target_name}' 属于破坏性高危操作（解散群/删除账号/批量清理/直播断流禁推），已完成安全审计记录。"
                }
            return res
        except Exception as e:
            return {
                "ActionStatus": "FAIL",
                "ErrorCode": -99,
                "ErrorInfo": f"调用接口 {target_name} 异常: {e!s}"
            }


class TimCallTool(CloudCallTool):
    """向后兼容别名：老技能或旧 Prompt 调用 tim_call 时无缝代理至 CloudCallTool"""
    name = "tim_call"
    description = "【向后兼容】调用腾讯云接口。等价于 cloud_call。"
    inputs = {
        "api_path": {
            "type": "string",
            "description": "接口路径或 Action 名称"
        },
        "payload": {
            "type": "object",
            "description": "请求参数 JSON 字典"
        }
    }

    def __init__(
        self,
        tim_client: TimClient | None = None,
        approval_requester: Any | None = None,
        bot_manager: BotManager | None = None,
        cloud_client: Any | None = None
    ):
        super().__init__(
            approval_requester=approval_requester,
            bot_manager=bot_manager,
            tim_client=tim_client,
            cloud_client=cloud_client
        )

    skip_forward_signature_validation = True

    def forward(self, api_path: str, payload: dict) -> dict:  # type: ignore[override]
        return CloudCallTool.forward(self, api_path, payload, service="im")


class CloudSearchApisTool(Tool):
    name = "cloud_search_apis"
    description = (
        "在腾讯云全量 API 知识库（涵盖云直播 CSS、即时通信 IM 及所有已挂载的多云业务标准库）中检索相关接口。"
        "返回匹配的接口 ID、名称、所属服务、Action/路径及功能简介。"
    )
    inputs = {
        "keyword": {
            "type": "string",
            "description": "搜索关键词，支持中文功能名、Action 名称、路径指令或领域词，如 '混流', '断流', '禁言', 'StreamState', '撤回'"
        },
        "service": {
            "type": "string",
            "description": "可选过滤服务：'live' (云直播), 'im' (即时通信), 'all' (全量搜索，默认)",
            "nullable": True
        }
    }
    output_type = "array"

    def forward(self, keyword: str, service: str | None = "all") -> list:
        from ..registry import registry
        registry.discover()

        kw = (keyword or "").lower().strip()
        svc = (service or "all").lower().strip()
        matches = []

        for api in registry._ordered_apis:
            st = getattr(api, "service_type", "im") or "im"
            if svc and svc != "all" and st != svc:
                continue

            name = getattr(api, "name", "").lower()
            cmd = getattr(api, "command", "").lower()
            act = getattr(api, "action", "").lower()
            desc = getattr(api, "description", "").lower()
            cat = getattr(api, "category", "").lower()
            subcat = getattr(api, "subcategory", "").lower()

            if kw in name or kw in cmd or kw in act or kw in desc or kw in cat or kw in subcat:
                matches.append({
                    "api_id": api.api_id,
                    "service": st,
                    "name": api.name,
                    "action_or_command": getattr(api, "action", "") or api.command,
                    "category": f"{api.category} / {api.subcategory}" if getattr(api, "subcategory", None) else api.category,
                    "description": (api.description or "")[:200]
                })

        return matches[:30]


class TimSearchApisTool(CloudSearchApisTool):
    """向后兼容别名"""
    name = "tim_search_apis"
    description = "【向后兼容】按关键词检索相关接口。等价于 cloud_search_apis。"


class CloudGetDocTool(Tool):
    name = "cloud_get_doc"
    description = (
        "获取指定腾讯云接口的完整官方文档规范、入参字段类型、复杂结构体定义、默认值与示例请求体。"
        "支持传入 Action 名称（如 'CreateCommonMixStream', 'DescribeLiveStreamState'）、command 路径（如 'openim/sendmsg'）或 api_id。"
    )
    inputs = {
        "api_identifier": {
            "type": "string",
            "description": "接口标识符：可以是 Action 名称（如 'CreateCommonMixStream'）、接口路径（如 'openim/sendmsg'）或 api_id"
        }
    }
    output_type = "object"

    def forward(self, api_identifier: str) -> dict:
        from ..registry import registry
        registry.discover()

        target = (api_identifier or "").strip()
        api = registry.get_api(target)

        if not api:
            return {"error": f"未找到接口文档: '{api_identifier}'。请先使用 cloud_search_apis 搜索匹配的接口名。"}

        fields_data = [f.to_dict() if hasattr(f, "to_dict") else f for f in getattr(api, "fields", [])]
        return {
            "api_id": api.api_id,
            "service": getattr(api, "service_type", "im"),
            "name": api.name,
            "action_or_command": getattr(api, "action", "") or api.command,
            "category": getattr(api, "category", ""),
            "subcategory": getattr(api, "subcategory", ""),
            "doc_url": getattr(api, "doc_url", ""),
            "description": getattr(api, "description", ""),
            "sample_payload": getattr(api, "sample_payload", {}),
            "fields": fields_data
        }


class TimGetDocTool(CloudGetDocTool):
    """向后兼容别名"""
    name = "tim_get_doc"
    description = "【向后兼容】获取接口文档规范。等价于 cloud_get_doc。"
    inputs = {
        "api_path": {
            "type": "string",
            "description": "接口标识符"
        }
    }

    skip_forward_signature_validation = True

    def forward(self, api_path: str) -> dict:  # type: ignore[override]
        return CloudGetDocTool.forward(self, api_path)


class EnhancePromptTool(Tool):
    name = "enhance_self_prompt"
    description = "BOT 自主增强自己的提示词准则（Prompt Self-Enhancement）。用于固化业务要求、规范或偏好，使该 BOT 在后续相似任务中表现更专业有效。"
    inputs = {
        "instruction": {
            "type": "string",
            "description": "需要追加的行为准则或处理规范，例如：'对于群禁言操作，若未指定时长默认按 3600 秒执行并在输出中告知'"
        },
        "reason": {
            "type": "string",
            "description": "为何增强该准则的原因说明，例如：'用户要求统一默认禁言1小时'",
            "nullable": True
        }
    }
    output_type = "string"

    def __init__(self, bot_id: str, bot_manager: BotManager):
        super().__init__()
        self.bot_id = bot_id
        self.bot_manager = bot_manager

    def forward(self, instruction: str, reason: str = "") -> str:
        guideline = self.bot_manager.add_learned_guideline(self.bot_id, instruction, reason)
        if guideline:
            return f"✅ 提示词已成功自我增强！新准则 ID: {guideline.id}，内容: {guideline.instruction}"
        return "❌ 增强提示词失败：未找到指定 BOT"


class SaveMemoryTool(Tool):
    name = "save_memory"
    description = "为当前 BOT 沉淀长期事实记忆，记录群 ID、管理员账号、特定业务配置或习惯。"
    inputs = {
        "key": {
            "type": "string",
            "description": "记忆的键名标识，如 'target_test_group', 'admin_id'"
        },
        "content": {
            "type": "string",
            "description": "记忆的具体事实内容"
        }
    }
    output_type = "string"

    def __init__(self, bot_id: str, bot_manager: BotManager):
        super().__init__()
        self.bot_id = bot_id
        self.bot_manager = bot_manager

    def forward(self, key: str, content: str) -> str:
        item = self.bot_manager.save_memory(self.bot_id, key, content)
        return f"🧠 长期记忆已保存：[{item.key}] -> {item.content}"


class RecallMemoryTool(Tool):
    name = "recall_memory"
    description = "检索当前 BOT 记住的长期事实清单。"
    inputs = {
        "query": {
            "type": "string",
            "description": "检索关键词，留空或 'all' 则返回所有记忆",
            "nullable": True
        }
    }
    output_type = "array"

    def __init__(self, bot_id: str, bot_manager: BotManager):
        super().__init__()
        self.bot_id = bot_id
        self.bot_manager = bot_manager

    def forward(self, query: str | None = None) -> list:
        mems = self.bot_manager.list_memories(self.bot_id)
        if not query or query.lower() in ("all", ""):
            return [m.model_dump() for m in mems]
        q = query.lower().strip()
        # 1. 优先尝试精确全子串匹配
        exact_matches = [m.model_dump() for m in mems if q in m.key.lower() or q in m.content.lower()]
        if exact_matches:
            return exact_matches

        # 2. 自然语言与多关键词模糊匹配 (中英文 Bigram 与词素)
        tokens = []
        words = [w for w in re.findall(r"[a-z0-9_\-]+", q) if len(w) >= 2]
        tokens.extend(words)

        cn_chars = re.findall(r"[\u4e00-\u9fa5]", q)
        for i in range(len(cn_chars) - 1):
            tokens.append(cn_chars[i] + cn_chars[i + 1])

        phrases = [p.strip() for p in re.split(r"[\s,，、/_\-]+", q) if 2 <= len(p.strip()) <= 6]
        tokens.extend(phrases)

        if not tokens:
            return []

        scored_memories = []
        for m in mems:
            k_low = m.key.lower()
            c_low = m.content.lower()
            match_score = sum(1 for tok in tokens if tok in k_low or tok in c_low)
            if match_score > 0:
                scored_memories.append((match_score, m.model_dump()))

        scored_memories.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in scored_memories]


class DeleteMemoryTool(Tool):
    name = "delete_memory"
    description = "删除当前 BOT 中过时或不再适用的记忆项。"
    inputs = {
        "key": {
            "type": "string",
            "description": "要删除的记忆键名"
        }
    }
    output_type = "string"

    def __init__(self, bot_id: str, bot_manager: BotManager):
        super().__init__()
        self.bot_id = bot_id
        self.bot_manager = bot_manager

    def forward(self, key: str) -> str:
        ok = self.bot_manager.delete_memory(self.bot_id, key)
        return f"记忆 [{key}] 删除成功" if ok else f"未找到记忆 [{key}]"


class SaveSkillTool(Tool):
    name = "save_skill"
    description = "BOT 将编写并验证成功的 Python 脚本沉淀为专属技能（Skill），供未来直接调度复用。"
    inputs = {
        "name": {
            "type": "string",
            "description": "技能名称，纯字母数字下划线，如 'batch_mute_group_members'"
        },
        "description": {
            "type": "string",
            "description": "技能的功能描述"
        },
        "python_code": {
            "type": "string",
            "description": "完整的 Python 脚本代码。脚本中可定义 def execute(tim_client, **kwargs) 函数或直接执行逻辑"
        },
        "input_schema": {
            "type": "object",
            "description": "技能入参规范说明，如 {'group_id': '目标群ID'}",
            "nullable": True
        }
    }
    output_type = "string"

    def __init__(self, bot_id: str, bot_manager: BotManager):
        super().__init__()
        self.bot_id = bot_id
        self.bot_manager = bot_manager

    def forward(self, name: str, description: str, python_code: str, input_schema: dict | None = None) -> str:
        skill = self.bot_manager.save_skill(self.bot_id, name, description, python_code, input_schema)
        return f"⚡ 技能 [{skill.name}] 已成功保存到当前 BOT 的专属技能库！后续可通过 run_skill('{skill.name}') 直接调用。"


class ListSkillsTool(Tool):
    name = "list_skills"
    description = "查看当前 BOT 已沉淀的所有技能列表及入参规范。"
    inputs = {}
    output_type = "array"

    def __init__(self, bot_id: str, bot_manager: BotManager):
        super().__init__()
        self.bot_id = bot_id
        self.bot_manager = bot_manager

    def forward(self) -> list:
        skills = self.bot_manager.list_skills(self.bot_id)
        return [
            {
                "name": s.name,
                "description": s.description,
                "input_schema": s.input_schema,
                "created_at": s.created_at
            }
            for s in skills
        ]


def build_sandbox_scope(
    caller_tool: Any,
    search_tool: Any | None = None,
    doc_tool: Any | None = None,
    cloud_client: Any | None = None,
    tim_client: Any | None = None,
    extra_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    通用沙箱环境作用域构建工厂 (Sandbox Scope Factory)
    动态根据已注册服务批量挂载快捷调用宏 ({service}_call)，自动消除硬编码
    """
    from ..live_util import generate_live_play_urls, generate_live_push_url
    from ..registry import registry

    scope: dict[str, Any] = {
        "cloud_call": caller_tool.forward,
        "search_apis": search_tool.forward if search_tool else None,
        "get_doc": doc_tool.forward if doc_tool else None,
        "gen_live_push_url": generate_live_push_url,
        "gen_live_play_url": generate_live_play_urls,
    }

    if cloud_client is not None:
        scope["cloud_client"] = cloud_client
    if tim_client is not None:
        scope["tim_client"] = tim_client

    # 动态为所有已注册服务挂载快捷调用宏，如 live_call, im_call, trtc_call, vod_call 等
    registry.discover()
    for svc in registry.list_services():
        svc_id = svc.get("id")
        if not svc_id:
            continue
        scope[f"{svc_id}_call"] = (lambda s=svc_id: lambda action_or_path, payload=None: caller_tool.forward(action_or_path, payload or {}, service=s))()

    # 向后兼容老指令别名
    scope["tim_call"] = scope.get("im_call") or (lambda path, payload=None: caller_tool.forward(path, payload or {}, service="im"))

    if extra_scope:
        scope.update(extra_scope)

    return scope


_SKILL_ALLOWED_IMPORTS = frozenset({
    "csv",
    "datetime",
    "json",
    "math",
    "random",
    "re",
    "time",
})
_SKILL_BLOCKED_NAMES = frozenset({
    "__import__",
    "breakpoint",
    "compile",
    "delattr",
    "eval",
    "exec",
    "getattr",
    "globals",
    "help",
    "input",
    "locals",
    "open",
    "setattr",
    "vars",
})
_SKILL_BLOCKED_ATTRIBUTES = frozenset({
    "__bases__",
    "__builtins__",
    "__class__",
    "__code__",
    "__dict__",
    "__getattribute__",
    "__globals__",
    "__mro__",
    "__subclasses__",
    "__setattr__",
})
_SKILL_TIMEOUT_SECONDS = 25.0
_MISSING = object()


def _validate_skill_ast(code: str) -> None:
    """Reject imports/reflection that cannot be safely exposed to a skill worker."""
    parsed_ast = ast.parse(code)
    for node in ast.walk(parsed_ast):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name.split(".", 1)[0]
                if module not in _SKILL_ALLOWED_IMPORTS:
                    raise ValueError(f"技能代码禁止导入模块 '{alias.name}'")
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".", 1)[0]
            if module not in _SKILL_ALLOWED_IMPORTS:
                raise ValueError(f"技能代码禁止从模块 '{node.module}' 导入")
        elif isinstance(node, ast.Name) and node.id in _SKILL_BLOCKED_NAMES:
            raise ValueError(f"技能代码禁止使用危险内置函数 '{node.id}'")
        elif isinstance(node, ast.Attribute):
            if node.attr in _SKILL_BLOCKED_ATTRIBUTES or (node.attr.startswith("__") and node.attr != "__name__"):
                raise ValueError(f"技能代码禁止访问内省属性 '{node.attr}'")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _SKILL_BLOCKED_NAMES:
            raise ValueError(f"技能代码禁止调用危险内置函数 '{node.func.id}'")


def _skill_safe_builtins() -> dict:
    import builtins

    return {
        name: value
        for name, value in builtins.__dict__.items()
        if name not in _SKILL_BLOCKED_NAMES and name != "__import__"
    }


def _skill_rpc_call(conn, name: str, args: tuple, kwargs: dict):
    conn.send({"kind": "call", "name": name, "args": args, "kwargs": kwargs})
    while True:
        response = conn.recv()
        if response.get("kind") != "response":
            continue
        if response.get("ok"):
            return response.get("value")
        raise RuntimeError(response.get("error", "技能 RPC 调用失败"))


def _skill_rpc_callable(conn, name: str):
    def call(*args, **kwargs):
        return _skill_rpc_call(conn, name, args, kwargs)

    return call


class _SkillTimClientProxy:
    __slots__ = ("_conn",)

    def __init__(self, conn):
        self._conn = conn

    def __call__(self, api_path: str = "", payload: dict | None = None, timeout: int = 15):
        return _skill_rpc_call(self._conn, "tim_client.call", (api_path, payload or {}, timeout), {})

    def invoke(self, service_name: str, command: str, payload: dict, timeout: int = 15):
        return _skill_rpc_call(self._conn, "tim_client.invoke", (service_name, command, payload, timeout), {})

    def get_usersig(self):
        return _skill_rpc_call(self._conn, "tim_client.get_usersig", (), {})


_SkillTimClientProxy.__name__ = "TimClient"


class _SkillCloudClientProxy:
    __slots__ = ("_conn",)

    def __init__(self, conn):
        self._conn = conn

    def invoke_api(self, action_or_path: str, payload: dict | None = None, service: str | None = None):
        return _skill_rpc_call(
            self._conn,
            "cloud_call",
            (action_or_path, payload or {}, service),
            {},
        )

    @property
    def tim_client(self):
        return _SkillTimClientProxy(self._conn)


_SkillCloudClientProxy.__name__ = "UniversalCloudClient"


def _skill_dependency(name: str, scope: dict):
    if name in ("client", "cloud_client"):
        return scope["cloud_client"]
    if name == "tim_client":
        return scope["tim_client"]
    if name in ("cloud_call", "tim_call"):
        return scope[name]
    if name in scope and name in {"args", "kwargs", "result", "gen_live_push_url", "gen_live_play_url"}:
        return scope[name]
    if name.endswith("_call") and name in scope:
        return scope[name]
    return _MISSING


def _invoke_skill_callable(execute, args: dict, scope: dict):
    signature = inspect.signature(execute)
    positional = []
    keyword = dict(args)
    has_var_keyword = False

    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            has_var_keyword = True
            continue
        if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            continue
        if parameter.name in keyword:
            value = keyword.pop(parameter.name)
        else:
            value = _skill_dependency(parameter.name, scope)
            if value is _MISSING:
                if parameter.default is not inspect.Parameter.empty:
                    continue
                continue
        if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
            positional.append(value)
        else:
            keyword[parameter.name] = value

    if has_var_keyword:
        keyword.update(args)
    return execute(*positional, **keyword)


def _skill_worker_entry(conn, code: str, args: dict, service_names: list):
    """Execute a skill in a killable worker with only RPC-backed capabilities."""
    try:
        with suppress(Exception):
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (int(_SKILL_TIMEOUT_SECONDS) + 2, int(_SKILL_TIMEOUT_SECONDS) + 2))
            resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))

        def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
            module = str(name).split(".", 1)[0]
            if module not in _SKILL_ALLOWED_IMPORTS:
                raise ImportError(f"安全拦截：禁止导入模块 '{name}'")
            return __import__(name, globals, locals, fromlist, level)

        safe_builtins = _skill_safe_builtins()
        safe_builtins["__import__"] = safe_import
        tim_client = _SkillTimClientProxy(conn)
        cloud_client = _SkillCloudClientProxy(conn)
        scope = {
            "__builtins__": safe_builtins,
            "args": args,
            "kwargs": args,
            "result": None,
            "tim_client": tim_client,
            "cloud_client": cloud_client,
            "cloud_call": _skill_rpc_callable(conn, "cloud_call"),
            "tim_call": _skill_rpc_callable(conn, "tim_call"),
            "im_call": _skill_rpc_callable(conn, "im_call"),
        }
        from ..live_util import generate_live_play_urls, generate_live_push_url
        scope["gen_live_push_url"] = generate_live_push_url
        scope["gen_live_play_url"] = generate_live_play_urls
        for service_name in service_names:
            scope[f"{service_name}_call"] = _skill_rpc_callable(conn, f"service:{service_name}")

        import builtins as _builtins
        _builtins.exec(code, scope, scope)  # noqa: S102

        execute = scope.get("execute")
        if callable(execute):
            result = _invoke_skill_callable(execute, args, scope)
            if inspect.isawaitable(result):
                result = asyncio.run(cast(Coroutine[Any, Any, Any], result))
        else:
            result = scope.get("result", "Skill executed successfully")
        conn.send({"kind": "result", "ok": True, "value": result})
    except BaseException as exc:
        with suppress(Exception):
            conn.send({"kind": "result", "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        conn.close()


class RunSkillTool(Tool):
    name = "run_skill"
    description = "运行当前 BOT 已沉淀的某个专属技能。"
    inputs = {
        "name": {
            "type": "string",
            "description": "技能名称"
        },
        "kwargs": {
            "type": "object",
            "description": "传递给该技能的入参字典",
            "nullable": True
        }
    }
    output_type = "object"

    def __init__(
        self,
        bot_id: str,
        bot_manager: BotManager,
        tim_client: TimClient,
        cloud_client: Any | None = None,
        approval_requester: Any | None = None
    ):
        super().__init__()
        self.bot_id = bot_id
        self.bot_manager = bot_manager
        self.tim_client = tim_client
        self.approval_requester = approval_requester
        if cloud_client is not None:
            self.cloud_client = cloud_client
        else:
            from ..cloud_client import UniversalCloudClient
            self.cloud_client = UniversalCloudClient()

    def _dispatch_skill_rpc(self, caller_tool: CloudCallTool, request: dict):
        name = request.get("name", "")
        args = request.get("args", ())
        kwargs = request.get("kwargs", {})
        if name == "cloud_call":
            return caller_tool.forward(*args, **kwargs)
        if name in ("tim_call", "im_call"):
            path = args[0] if args else kwargs.get("path", "")
            payload = args[1] if len(args) > 1 else kwargs.get("payload", {})
            return caller_tool.forward(path, payload or {}, service="im")
        if name == "tim_client.call":
            api_path, payload, timeout = args
            return caller_tool.forward(api_path, payload or {}, service="im")
        if name == "tim_client.invoke":
            service_name, command, payload, _timeout = args
            path = f"{service_name}/{command}" if service_name else command
            return caller_tool.forward(path, payload or {}, service="im")
        if name == "tim_client.get_usersig":
            return self.tim_client.get_usersig()
        if name.startswith("service:"):
            service = name.split(":", 1)[1]
            action_or_path = args[0] if args else kwargs.get("action_or_path", "")
            payload = args[1] if len(args) > 1 else kwargs.get("payload", {})
            return caller_tool.forward(action_or_path, payload or {}, service=service)
        raise RuntimeError(f"技能不允许调用未注册能力: {name}")

    def _run_isolated(self, skill_code: str, args: dict, caller_tool: CloudCallTool) -> dict:
        from ..registry import registry

        try:
            registry.discover()
            service_names = [item.get("id") for item in registry.list_services() if item.get("id")]
        except Exception:
            service_names = []

        context = multiprocessing.get_context("spawn")
        parent_conn, child_conn = context.Pipe()
        process = context.Process(
            target=_skill_worker_entry,
            args=(child_conn, skill_code, args, service_names),
            daemon=True,
        )
        approval_elapsed = 0.0
        approval_started_at = None
        previous_approval_callback = getattr(caller_tool, "approval_wait_callback", None)

        def mark_approval_waiting(waiting: bool):
            nonlocal approval_elapsed, approval_started_at
            if waiting:
                approval_started_at = time.monotonic()
            elif approval_started_at is not None:
                approval_elapsed += time.monotonic() - approval_started_at
                approval_started_at = None

        caller_tool.approval_wait_callback = mark_approval_waiting
        try:
            process.start()
            child_conn.close()
        except Exception as exc:
            caller_tool.approval_wait_callback = previous_approval_callback
            child_conn.close()
            parent_conn.close()
            return {"success": False, "error": f"技能隔离进程启动失败: {exc}"}

        deadline = time.monotonic() + _SKILL_TIMEOUT_SECONDS
        result_message = None
        timed_out = False
        try:
            while time.monotonic() < deadline:
                if parent_conn.poll(0.05):
                    message = parent_conn.recv()
                    if message.get("kind") == "call":
                        try:
                            value = self._dispatch_skill_rpc(caller_tool, message)
                            parent_conn.send({"kind": "response", "ok": True, "value": value})
                        except Exception as exc:
                            parent_conn.send({"kind": "response", "ok": False, "error": str(exc)})
                        finally:
                            # Human approval time must not consume the skill deadline.
                            if approval_elapsed:
                                deadline += approval_elapsed
                                approval_elapsed = 0.0
                    elif message.get("kind") == "result":
                        result_message = message
                        break
                if not process.is_alive():
                    while parent_conn.poll():
                        message = parent_conn.recv()
                        if message.get("kind") == "result":
                            result_message = message
                    break
            else:
                timed_out = True
        except (EOFError, OSError) as exc:
            if result_message is None:
                result_message = {"kind": "result", "ok": False, "error": str(exc)}
        finally:
            caller_tool.approval_wait_callback = previous_approval_callback
            if timed_out or process.is_alive():
                process.terminate()
                process.join(1.0)
                if process.is_alive() and hasattr(process, "kill"):
                    process.kill()
                    process.join(1.0)
            else:
                process.join(1.0)
            parent_conn.close()

        if timed_out:
            return {"success": False, "error": "执行技能超时，隔离进程已终止（超过 25 秒限制）"}
        if not result_message:
            return {"success": False, "error": "技能隔离进程异常退出"}
        if result_message.get("ok"):
            return {"success": True, "result": result_message.get("value")}
        return {"success": False, "error": f"执行技能失败: {result_message.get('error', '未知错误')}"}

    def forward(self, name: str, kwargs: Any | None = None) -> dict:
        skill = self.bot_manager.get_skill(self.bot_id, name)
        if not skill:
            return {"error": f"未找到技能: {name}"}

        args = kwargs
        if isinstance(args, str):
            try:
                args = json.loads(args.strip())
            except Exception:
                try:
                    parsed = ast.literal_eval(args.strip())
                    args = parsed if isinstance(parsed, dict) else {}
                except Exception:
                    args = {}
        if not isinstance(args, dict):
            args = args or {}

        try:
            _validate_skill_ast(skill.code)
        except (SyntaxError, ValueError) as exc:
            return {"success": False, "error": f"安全拦截：{exc}"}
        except Exception as exc:
            return {"success": False, "error": f"技能代码语法解析失败: {exc}"}

        caller_tool = CloudCallTool(
            approval_requester=self.approval_requester,
            bot_manager=self.bot_manager,
            tim_client=self.tim_client,
            cloud_client=self.cloud_client,
        )
        return self._run_isolated(skill.code, args, caller_tool)


class CreateBotTool(Tool):
    name = "create_bot"
    description = (
        "【仅内建全能助手独享特权】创建并孵化新的专属业务智能体 (Bot)。"
        "当用户要求创建、新增或孵化新的智能体时（如'帮我建一个社群风控官'、'创建一个IM巡检机器人'）调用此工具。"
        "可指定名称、业务目标/定位、头像Emoji、系统设定提示词及推荐指令。"
    )
    inputs = {
        "name": {
            "type": "string",
            "description": "智能体名称，例如 '社群风控官', '消息管理员', 'IM运维巡检官'"
        },
        "goal": {
            "type": "string",
            "description": "智能体的主要业务职责定位与应用场景，例如 '负责监控大群敏感发言、违规撤回与批量禁言处理'",
            "nullable": True
        },
        "avatar": {
            "type": "string",
            "description": "智能体头像 Emoji，如 '🛡️', '🔍', '📊', '⚡'。若不提供将自动推断",
            "nullable": True
        },
        "base_prompt": {
            "type": "string",
            "description": "智能体系统设定提示词（约200字，专业严谨人设，结合腾讯云多业务全量接口与沙箱能力）。若不提供将根据名称和目标自动生成",
            "nullable": True
        },
        "suggested_prompts": {
            "type": "array",
            "description": "可选：为新智能体预设的 3~4 条典型业务快捷指令",
            "nullable": True
        }
    }
    output_type = "object"

    def __init__(self, bot_manager: BotManager):
        super().__init__()
        self.bot_manager = bot_manager

    def forward(
        self,
        name: str,
        goal: str | None = None,
        avatar: str | None = None,
        base_prompt: str | None = None,
        suggested_prompts: list | None = None
    ) -> dict:
        clean_name = name.strip()
        if not clean_name:
            return {"success": False, "error": "智能体名称不能为空"}

        profile = self.bot_manager.generate_bot_profile(clean_name, goal or "")
        target_id = profile["id"]
        # 避免重名 ID 冲突
        if self.bot_manager.get_bot(target_id):
            target_id = f"{target_id}_{int(time.time()) % 1000}"

        target_avatar = avatar or profile.get("avatar", "🤖")
        target_desc = goal or profile.get("description", "")
        target_prompt = base_prompt or profile.get("base_prompt", "")
        target_suggested = suggested_prompts or profile.get("suggested_prompts", [])

        # 清洗可能存在的 ChatMessage repr
        target_prompt = BotManager.clean_prompt_text(target_prompt)

        new_bot = self.bot_manager.create_bot(
            bot_id=target_id,
            name=clean_name,
            avatar=target_avatar,
            description=target_desc,
            base_prompt=target_prompt,
            suggested_prompts=target_suggested
        )

        return {
            "success": True,
            "message": f"🎉 专属智能体【{new_bot.name}】创建成功！已成功入驻工作室，可随时在左侧切换或在对话中通过 @{new_bot.name} 进行协同调度。",
            "bot": {
                "id": new_bot.id,
                "name": new_bot.name,
                "avatar": new_bot.avatar,
                "description": new_bot.description,
                "suggested_prompts": new_bot.suggested_prompts
            }
        }


class ListAgentsTool(Tool):
    name = "list_agents"
    description = "查看当前工作室入驻的所有智能体清单（包含名称、ID、头像、职责定位与技能数），用于发现其他智能体以便进行协同指挥调度。"
    inputs = {}
    output_type = "array"

    def __init__(self, bot_manager: BotManager):
        super().__init__()
        self.bot_manager = bot_manager

    def forward(self) -> list:
        bots = self.bot_manager.list_bots()
        return [
            {
                "id": b["id"],
                "name": b.get("name", ""),
                "avatar": b.get("avatar", "🤖"),
                "description": b.get("description", ""),
                "skills_count": b.get("skills_count", 0),
                "mention_handle": f"@{b.get('name', '')}"
            }
            for b in bots
        ]


class CallAgentTool(Tool):
    name = "call_agent"
    description = (
        "指挥或协同其他智能体处理专业子任务（智能体协同/调度指挥机制）。"
        "支持通过智能体名称（例如 '社群风控官'、'@社群风控官'）或智能体 ID 定向派发指令。"
        "目标智能体将独立结合其专属系统提示词、长期记忆与工具技能执行该任务，并返回其分析结论与执行产物。"
    )
    inputs = {
        "agent_name": {
            "type": "string",
            "description": "目标智能体名称或 ID，如 '社群风控官', '消息管理员', '@IM巡检官'"
        },
        "task": {
            "type": "string",
            "description": "派发给该智能体的具体任务描述或要求"
        }
    }
    output_type = "string"

    def __init__(
        self,
        bot_id: str,
        bot_manager: BotManager,
        engine: Any | None = None,
        call_chain: list[str] | None = None,
        approval_requester: Any | None = None,
    ):



        super().__init__()
        self.bot_id = bot_id
        self.bot_manager = bot_manager
        self.engine = engine
        self.call_chain = call_chain or [bot_id]
        self.approval_requester = approval_requester

    def forward(self, agent_name: str, task: str) -> str:
        raw_name = (agent_name or "").strip()
        # 剥除各种标点符号、前缀与外包围符: @, #, 【】, [], "", '', 括号等
        clean_name = re.sub(r"^[@#【\[\"'（(]+", "", raw_name)
        clean_name = re.sub(r"[@#】\]\"'）)]+$", "", clean_name).strip()

        # 如果传入了形如 "消息巡检员 (bot_inspector_9870)" 或 "社群风控官(security_bot)"，抽取 ID 或主要名称
        id_match = re.search(r"[\(（]([a-zA-Z0-9_-]+)[\)）]", clean_name)
        explicit_id = id_match.group(1) if id_match else None
        if explicit_id:
            clean_name = re.sub(r"[\(（][a-zA-Z0-9_-]+[\)）]", "", clean_name).strip()

        all_bots = self.bot_manager.list_bots()
        target_bot = None

        # 1. 显式 ID 匹配
        if explicit_id:
            target_bot = self.bot_manager.get_bot(explicit_id)

        # 2. 精确 ID 或名称匹配
        if not target_bot:
            for b in all_bots:
                b_id = b.get("id", "")
                b_name = b.get("name", "")
                if b_id == clean_name or b_name == clean_name or b_name.lower() == clean_name.lower():
                    target_bot = self.bot_manager.get_bot(b_id)
                    break

        # 3. 子串双向包含匹配 (如 "风控官" in "社群风控官", "巡检" in "消息巡检员")
        if not target_bot and clean_name:
            for b in all_bots:
                b_id = b.get("id", "")
                b_name = b.get("name", "")
                if clean_name in b_name or b_name in clean_name or clean_name.lower() in b_id.lower():
                    target_bot = self.bot_manager.get_bot(b_id)
                    break

        # 4. 语义核心关键词/领域匹配 (自然语言口语化称谓如 "风控", "巡检", "管理员", "投研", "运维")
        if not target_bot and len(clean_name) >= 2:
            keywords = ["风控", "安全", "巡检", "监控", "管理", "对账", "投研", "研报", "运维", "测试", "客服"]
            matched_kws = [k for k in keywords if k in clean_name]
            for kw in matched_kws:
                for b in all_bots:
                    b_name = b.get("name", "")
                    b_desc = b.get("description", "")
                    if kw in b_name or kw in b_desc:
                        target_bot = self.bot_manager.get_bot(b.get("id", ""))
                        break
                if target_bot:
                    break

        if not target_bot:
            available = ", ".join([f"【{b.get('name', '')}】" for b in all_bots])
            return f"❌ 未能通过「{agent_name}」识别到对应智能体。当前团队入驻的智能体伙伴有：{available}。请告知目标智能体名称。"

        if target_bot.id == self.bot_id:
            return f"⚠️ 智能体「{target_bot.name}」即为当前智能体自身，无需通过 call_agent 自调用，请直接在当前流程中执行该任务。"

        if not self.engine:
            return f"⚠️ 系统执行引擎未就绪，无法派发给智能体【{target_bot.name}】。"

        t0 = time.time()
        caller_bot = self.bot_manager.get_bot(self.bot_id)
        caller_name = caller_bot.name if caller_bot else self.bot_id

        try:
            result = self.engine.execute_subtask(
                caller_bot_id=self.bot_id,
                target_bot_id=target_bot.id,
                task=task,
                call_chain=self.call_chain,
                approval_requester=self.approval_requester,
            )
            elapsed = round(time.time() - t0, 2)
            preview = result.strip()
            if len(preview) > 160:
                preview = preview[:160] + "..."

            record = {
                "caller_bot_id": self.bot_id,
                "caller_bot_name": caller_name,
                "target_bot_id": target_bot.id,
                "target_bot_name": target_bot.name,
                "target_avatar": target_bot.avatar or "🤖",
                "task": task,
                "status": "success",
                "result_preview": preview,
                "elapsed_seconds": elapsed,
                "timestamp": time.strftime("%H:%M:%S")
            }
            if hasattr(self.engine, "record_orchestration"):
                self.engine.record_orchestration(record)

            return (
                f"🤖【{target_bot.name}】执行与协同反馈：\n\n"
                f"{result.strip()}\n"
            )
        except Exception as e:
            elapsed = round(time.time() - t0, 2)
            err_msg = str(e)
            record = {
                "caller_bot_id": self.bot_id,
                "caller_bot_name": caller_name,
                "target_bot_id": target_bot.id,
                "target_bot_name": target_bot.name,
                "target_avatar": target_bot.avatar or "🤖",
                "task": task,
                "status": "error",
                "result_preview": f"异常: {err_msg}",
                "elapsed_seconds": elapsed,
                "timestamp": time.strftime("%H:%M:%S")
            }
            if hasattr(self.engine, "record_orchestration"):
                self.engine.record_orchestration(record)

            return f"❌ 调度智能体【{target_bot.name}】执行子任务时发生异常: {err_msg}"


class ReadFileTool(Tool):
    name = "read_file"
    description = "读取本地文本文件、代码文件、文档（DOCX/PDF/TXT/CSV/JSON/LOG）或已上传附件的文本内容。支持传入文件绝对路径或上传文件名。"
    inputs = {
        "file_path": {
            "type": "string",
            "description": "文件绝对路径或上传文件名"
        },
        "max_chars": {
            "type": "integer",
            "description": "最大读取字符数，默认 4000",
            "nullable": True
        }
    }
    output_type = "string"

    def __init__(self, bot_id: str | None = None, bot_manager: BotManager | None = None):
        super().__init__()
        self.bot_id = bot_id
        self.bot_manager = bot_manager

    def forward(self, file_path: str, max_chars: int | None = 4000) -> str:
        limit = max_chars or 4000
        path = file_path.strip()
        # 若是相对路径或仅文件名，优先在当前 BOT 的 uploads 目录寻找
        if not os.path.isabs(path) and self.bot_id and self.bot_manager:
            bot_dir = self.bot_manager._bot_dir(self.bot_id)
            uploads_dir = os.path.join(bot_dir, "uploads")
            candidate = os.path.join(uploads_dir, os.path.basename(path))
            if os.path.exists(candidate):
                path = candidate
            elif os.path.exists(uploads_dir):
                try:
                    upload_names = os.listdir(uploads_dir)
                except OSError:
                    upload_names = []
                for fn in upload_names:

                    if fn.endswith(path) or path in fn:
                        path = os.path.join(uploads_dir, fn)
                        break

        if not os.path.exists(path):
            return f"❌ 文件不存在: '{file_path}'"

        from .file_parser import (
            get_file_category_and_type,
            parse_file_summary,
            read_text_with_encoding,
        )
        cat, ftype = get_file_category_and_type(os.path.basename(path))
        if ftype == "docx":
            try:
                import docx
                doc = docx.Document(path)
                paras = [p.text for p in doc.paragraphs if p.text]
                full_text = "\n\n".join(paras)
                if len(full_text) > limit:
                    return full_text[:limit] + f"\n\n[... DOCX 内容已截断，共读取前 {limit} 字符]"
                return full_text or "文档内容为空。"
            except Exception as e:
                return f"读取 DOCX 失败: {e!s}"
        elif ftype == "pdf":
            try:
                import pypdf
                reader = pypdf.PdfReader(path)
                pages_text = [p.extract_text() for p in reader.pages if p.extract_text()]
                full_text = "\n\n".join(pages_text)
                if len(full_text) > limit:
                    return full_text[:limit] + f"\n\n[... PDF 内容已截断，共读取前 {limit} 字符]"
                return full_text or "PDF 内容为空或仅包含扫描图片。"
            except Exception as e:
                return f"读取 PDF 失败: {e!s}"
        elif ftype in ("xlsx", "xls", "csv"):
            summary_info = parse_file_summary(path, os.path.basename(path))
            return summary_info.get("summary", "")

        text = read_text_with_encoding(path, max_chars=limit)
        return text or "文件内容为空。"


class SearchKnowledgeTool(Tool):
    name = "search_knowledge"
    description = (
        "在当前智能体挂载的企业私有知识库、运维手册与业务规范中检索相关文档段落。"
        "当遇到腾讯云 IM 错误排查规范、特定内部群规、违规处置流程、网络故障排查 SOP 或私有配置要求时，调用此工具获取精准规范依据。"
    )
    inputs = {
        "query": {
            "type": "string",
            "description": "检索关键词或问题描述，例如 '解散群规范', '错误码10007排查', '敏感词处置流程'"
        }
    }
    output_type = "string"

    def __init__(self, bot_id: str, rag_mgr: Any | None = None):
        super().__init__()
        self.bot_id = bot_id
        from .rag_manager import rag_manager
        self.rag_mgr = rag_mgr or rag_manager

    def forward(self, query: str) -> str:
        results = self.rag_mgr.search(self.bot_id, query, top_k=4)
        if not results:
            return f"未在企业知识库中检索到与「{query}」相关的文档段落。请尝试更通用的关键词或查阅官方接口文档。"

        lines = [f"🔍 在企业私有知识库中找到 {len(results)} 条相关依据：\n"]
        for idx, r in enumerate(results, 1):
            lines.append(f"【{idx}】《{r.get('doc_name')}》- {r.get('title')} (匹配度: {r.get('score')}):")
            lines.append(f"{r.get('content')}\n")
        return "\n".join(lines)


def build_bot_tools(
    bot_id: str,
    bot_manager: BotManager,
    tim_client: TimClient,
    engine: Any | None = None,
    call_chain: list[str] | None = None,
    approval_requester: Any | None = None,
    cloud_client: Any | None = None
) -> list[Tool]:
    """为指定 BOT 组装能力工具集（涵盖云直播 API 3.0 与即时通信 IM REST API）"""
    tools = [
        CloudCallTool(
            approval_requester=approval_requester,
            bot_manager=bot_manager,
            tim_client=tim_client,
            cloud_client=cloud_client
        ),
        CloudSearchApisTool(),
        CloudGetDocTool(),
        TimCallTool(
            tim_client,
            approval_requester=approval_requester,
            bot_manager=bot_manager,
            cloud_client=cloud_client
        ),
        TimSearchApisTool(),
        TimGetDocTool(),
        EnhancePromptTool(bot_id, bot_manager),
        SaveMemoryTool(bot_id, bot_manager),
        RecallMemoryTool(bot_id, bot_manager),
        DeleteMemoryTool(bot_id, bot_manager),
        SaveSkillTool(bot_id, bot_manager),
        ListSkillsTool(bot_id, bot_manager),
        RunSkillTool(
            bot_id,
            bot_manager,
            tim_client,
            cloud_client=cloud_client,
            approval_requester=approval_requester
        ),
        ListAgentsTool(bot_manager),
        CallAgentTool(bot_id, bot_manager, engine, call_chain, approval_requester),
        ReadFileTool(bot_id, bot_manager),
        SearchKnowledgeTool(bot_id)
    ]
    # 核心限制：仅内建全能助手 (default_assistant) 独享新建智能体的能力
    if bot_id == "default_assistant":
        tools.append(CreateBotTool(bot_manager))
    return tools
