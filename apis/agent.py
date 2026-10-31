# ruff: noqa
"""
智能体 REST & SSE 路由：多 BOT 管理、流式对话、记忆库、自编技能与自增强准则
"""

import asyncio
import base64
import ipaddress
import json
import logging
import mimetypes
import os
import re
import socket
import time
from contextlib import suppress
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

import requests
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from core.agent.bot_manager import bot_manager
from core.agent.constants import SUPPORTED_PROVIDERS, BOT_PRESET_TEMPLATES, HIGH_RISK_API_KEYWORDS
from core.agent.engine import agent_engine
from core.agent.file_parser import parse_file_summary, format_file_size
from core.agent.models import BotConfig, LLMConfig
from core.agent.rag_manager import rag_manager
from core.agent.run_manager import AgentRun, MAX_EVENTS_PER_RUN, agent_run_manager
from core.tim_client import TimClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["AI 智能体"])

MAX_AGENT_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_KNOWLEDGE_UPLOAD_BYTES = 20 * 1024 * 1024


# -------------------------------------------------------------
# DTO 请求体定义
# -------------------------------------------------------------
class LLMConfigPayload(BaseModel):
    provider: str = Field(..., description="模型提供商：deepseek, qwen, ollama, openai, custom")
    model_name: str = Field(..., description="模型名称")
    base_url: str = Field(default="", description="Base URL")
    api_key: Optional[str] = Field(default="", description="API Key")
    temperature: float = Field(default=0.2, description="温度")
    max_steps: Optional[int] = Field(default=30, description="最大执行步数（推荐 20~50 轮）")
    context_budget: Optional[int] = Field(default=32000, description="即时工作记忆总字符预算")
    max_context_turns: Optional[int] = Field(default=25, description="即时工作记忆保留最大轮数")


class FetchModelsPayload(BaseModel):
    provider: Optional[str] = Field(default=None, description="模型提供商")
    base_url: Optional[str] = Field(default=None, description="Base URL")
    api_key: Optional[str] = Field(default=None, description="API Key")


class CreateBotPayload(BaseModel):
    name: str = Field(..., description="智能体名称")
    goal: Optional[str] = Field(default="", description="职责与业务目标（希望智能体做什么）")
    id: Optional[str] = Field(default=None, description="唯一标识 (可选，留空由 AI 自动生成)")
    avatar: Optional[str] = Field(default=None, description="头像 Emoji (可选，留空由 AI 自动匹配)")
    description: Optional[str] = Field(default=None, description="功能描述 (可选，留空由 AI 自动生成)")
    base_prompt: Optional[str] = Field(default=None, description="基础人设提示词 (可选，留空由 AI 自动生成)")
    suggested_prompts: Optional[List[str]] = Field(default=None, description="个性化推荐快捷指令")
    model_override: Optional[Dict[str, Any]] = Field(default=None, description="专属模型配置")


class GeneratePreviewPayload(BaseModel):
    name: str = Field(..., description="智能体名称")
    goal: Optional[str] = Field(default="", description="职责与业务目标")


class UpdateBotPayload(BaseModel):
    name: Optional[str] = None
    avatar: Optional[str] = None
    description: Optional[str] = None
    base_prompt: Optional[str] = None
    model_override: Optional[Dict[str, Any]] = None


class AttachmentItem(BaseModel):
    id: str = Field(..., description="附件唯一标识")
    name: str = Field(..., description="原始文件名")
    size: int = Field(default=0, description="文件大小（字节）")
    type: str = Field(default="file", description="文件格式类型，如 docx, xlsx, csv, txt, pdf, png 等")
    category: Optional[str] = Field(default="file", description="分类：document, table, text, image, code 等")
    path: str = Field(..., description="服务器本地文件绝对路径")
    url: Optional[str] = Field(default=None, description="在线访问/下载地址")
    data_url: Optional[str] = Field(default=None, description="用于图片快速缩略图的 Base64 Data URL")
    size_formatted: Optional[str] = Field(default=None, description="人性化大小表示")
    summary: Optional[str] = Field(default=None, description="结构化摘要")
    preview: Optional[str] = Field(default=None, description="预览文本")
    meta: Optional[Dict[str, Any]] = Field(default=None, description="结构元数据")


class ChatPayload(BaseModel):
    message: str = Field(default="", description="用户输入的指令或问题")
    client_msg_id: Optional[str] = Field(default=None, description="前端指定的消息ID")
    attachments: Optional[List[AttachmentItem]] = Field(default=None, description="随消息发送的附件列表")


class TruncateHistoryPayload(BaseModel):
    message_id: str = Field(..., description="截断至此消息 ID（保留该消息及之前内容，丢弃后续内容）")


class MemoryPayload(BaseModel):
    key: str = Field(..., description="记忆标识")
    content: str = Field(..., description="记忆事实详情")


class GuidelinePayload(BaseModel):
    instruction: str = Field(..., description="指令规则内容")
    reason: Optional[str] = Field(default="", description="强化原因")


class SaveSkillPayload(BaseModel):
    name: str = Field(..., description="技能名称")
    description: str = Field(..., description="功能描述")
    python_code: str = Field(..., description="Python 代码")
    input_schema: Optional[Dict[str, Any]] = Field(default_factory=dict, description="入参模式说明")


class RunSkillPayload(BaseModel):
    kwargs: Optional[Dict[str, Any]] = Field(default_factory=dict, description="执行入参")
    confirm_high_risk: bool = Field(default=False, description="明确确认执行高危技能")
    approved_high_risk_paths: List[str] = Field(
        default_factory=list,
        description="本次允许执行的高危 API 路径白名单；每次调用按路径逐项匹配",
    )


class ToggleKnowledgePayload(BaseModel):
    enabled: Optional[bool] = None


class SearchKnowledgePayload(BaseModel):
    query: str = Field(..., description="检索问题或关键词")
    top_k: Optional[int] = Field(default=4, description="返回最多匹配条数")
    min_score: Optional[float] = Field(default=0.05, description="最低相关度分数阈值")


class ApprovalDecisionPayload(BaseModel):
    decision: str = Field(..., description="决策：'approve' 或 'reject'")



# -------------------------------------------------------------
# LLM 全局模型配置与预设
# -------------------------------------------------------------
@router.get("/config")
def get_llm_config():
    """获取大模型配置（API Key 脱敏）与支持的供应商列表 (SSoT)"""
    return {
        "config": bot_manager.get_masked_llm_config(),
        "providers": SUPPORTED_PROVIDERS
    }


@router.get("/templates")
def list_bot_templates():
    """获取预设智能体角色模板（社群风控官、消息巡检员、运维保障官等）"""
    return {
        "success": True,
        "templates": BOT_PRESET_TEMPLATES
    }


@router.get("/security/high-risk-keywords")
def get_high_risk_keywords():
    """获取高危破坏性接口安全拦截规则与关键词"""
    return {
        "success": True,
        "keywords": bot_manager.get_high_risk_keywords(),
        "default_keywords": HIGH_RISK_API_KEYWORDS
    }


class HighRiskKeywordsPayload(BaseModel):
    keywords: List[str] = Field(..., description="高危接口拦截关键词列表")


@router.post("/security/high-risk-keywords")
def update_high_risk_keywords(payload: HighRiskKeywordsPayload):
    """更新高危破坏性接口拦截关键词"""
    updated = bot_manager.set_high_risk_keywords(payload.keywords)
    return {
        "success": True,
        "keywords": updated
    }


@router.post("/config")
def save_llm_config(payload: LLMConfigPayload):
    """保存大模型连接配置"""
    existing = bot_manager.get_llm_config()
    # 若前端传空 key 且原本已有 key，保留原 key 避免被误清空
    api_key = payload.api_key.strip() if payload.api_key is not None else ""
    if not api_key and existing.api_key and payload.provider == existing.provider:
        api_key = existing.api_key

    new_cfg = LLMConfig(
        provider=payload.provider,
        model_name=payload.model_name.strip(),
        base_url=payload.base_url.strip(),
        api_key=api_key,
        temperature=payload.temperature,
        max_steps=payload.max_steps if payload.max_steps and payload.max_steps >= 5 else 30,
        context_budget=payload.context_budget if payload.context_budget and payload.context_budget >= 4000 else 32000,
        max_context_turns=payload.max_context_turns if payload.max_context_turns and payload.max_context_turns >= 5 else 25,
    )
    bot_manager.save_llm_config(new_cfg)
    return {"success": True, "config": bot_manager.get_masked_llm_config()}


@router.post("/config/test")
def test_llm_connection():
    """测试当前大模型连通性"""
    cfg = bot_manager.get_llm_config()
    try:
        model = agent_engine.create_model(cfg)
        resp = model([{"role": "user", "content": "Ping"}])
        text = str(resp) if hasattr(resp, "__str__") else "OK"
        return {"success": True, "message": "大模型连接成功！", "response": text[:100]}
    except Exception as e:
        return {"success": False, "error": f"连接失败: {str(e)}"}


LOCAL_MODEL_ALLOWED_PORTS = {11434, 8080}


def _local_model_allowed_ports() -> set[int]:
    """Read an explicit local-model port allowlist without weakening defaults."""
    raw_ports = os.environ.get("LOCAL_MODEL_ALLOWED_PORTS", "")
    if not raw_ports.strip():
        return set(LOCAL_MODEL_ALLOWED_PORTS)
    ports: set[int] = set()
    for raw_port in raw_ports.split(","):
        try:
            port = int(raw_port.strip())
        except ValueError:
            continue
        if 1 <= port <= 65535:
            ports.add(port)
    return ports or set(LOCAL_MODEL_ALLOWED_PORTS)


def _validate_model_base_url(base_url: str) -> str:
    """Allow public model endpoints and Ollama's local port, reject SSRF targets."""
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=422, detail="Base URL 必须是有效的 http(s) 地址")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=422, detail="Base URL 不得包含账号密码")

    host = parsed.hostname.lower().rstrip(".")
    port = parsed.port
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if host in local_hosts:
        # Ollama defaults to 11434. Normalize omitted ports and permit an
        # intentionally configured custom local port.
        if port is None:
            netloc = parsed.hostname
            if ":" in netloc and not netloc.startswith("["):
                netloc = f"[{netloc}]"
            return urlunparse(parsed._replace(netloc=f"{netloc}:11434")).rstrip("/")
        if port not in _local_model_allowed_ports():
            allowed_ports = ", ".join(str(item) for item in sorted(_local_model_allowed_ports()))
            raise HTTPException(
                status_code=422,
                detail=f"本地模型端口不在允许列表，仅支持：{allowed_ports}；可通过 LOCAL_MODEL_ALLOWED_PORTS 配置",
            )
        return base_url.rstrip("/")

    try:
        addresses = {
            ipaddress.ip_address(info[4][0])
            for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Base URL 域名无法安全解析") from exc

    if not addresses or any(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        for address in addresses
    ):
        raise HTTPException(status_code=422, detail="禁止访问内网、环回、链路本地或保留地址")
    return base_url.rstrip("/")


@router.post("/models")
@router.get("/models")
def fetch_available_models(payload: Optional[FetchModelsPayload] = None):
    """
    通过目标提供商的 OpenAI 兼容 /models 接口或 Ollama /api/tags 动态获取可用模型标识列表。
    若远程探测失败或未配置 Key，则智能返回常见高频预设模型作为候选，保留完全自定义能力。
    """
    cfg = bot_manager.get_llm_config()
    provider = (payload.provider if payload and payload.provider else cfg.provider) or "deepseek"

    if payload and payload.base_url is not None:
        base_url = payload.base_url.strip()
    else:
        base_url = (cfg.base_url or "").strip()

    # 获取有效 API Key (避免使用带 ... 的脱敏 Key)
    req_key = payload.api_key.strip() if payload and payload.api_key is not None else ""
    if req_key and "..." not in req_key:
        api_key = req_key
    elif payload and payload.provider and payload.provider != cfg.provider and not req_key:
        api_key = ""
    else:
        api_key = cfg.api_key

    # 常见提供商官方推荐预设候选池
    PROVIDER_PRESETS = {
        "deepseek": ["deepseek-chat", "deepseek-reasoner", "deepseek-flash", "deepseek-v4-pro"],
        "qwen": ["qwen-plus", "qwen-max", "qwen-turbo", "qwen-long", "qwen2.5-coder-32b-instruct", "qwen2.5-72b-instruct"],
        "ollama": ["qwen2.5-coder:7b", "qwen2.5:7b", "llama3.1:8b", "deepseek-r1:8b"],
        "openai": ["gpt-4o", "gpt-4o-mini", "o1", "o3-mini", "gpt-4-turbo"],
        "custom": []
    }

    if not base_url:
        presets = PROVIDER_PRESETS.get(provider, [])
        return {
            "success": True,
            "models": presets,
            "source": "preset",
            "message": "Base URL 为空，返回该提供商默认推荐模型列表"
        }

    norm_base = _validate_model_base_url(base_url)
    candidates = []
    if provider == "ollama" or "11434" in norm_base:
        candidates.append(f"{norm_base}/api/tags")
        candidates.append(f"{norm_base}/v1/models")
        candidates.append(f"{norm_base}/models")
    else:
        candidates.append(f"{norm_base}/models")
        if not norm_base.endswith("/v1"):
            candidates.append(f"{norm_base}/v1/models")
        else:
            candidates.append(f"{norm_base[:-3]}/models")

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    found_models = []
    last_error = None

    for url in candidates:
        try:
            resp = requests.get(
                url,
                headers=headers,
                timeout=(3, 6),
                allow_redirects=False,
            )
            if resp.status_code == 200:
                data = resp.json()
                items = []
                if isinstance(data, dict):
                    if "data" in data and isinstance(data["data"], list):
                        items = data["data"]
                    elif "models" in data and isinstance(data["models"], list):
                        items = data["models"]
                elif isinstance(data, list):
                    items = data

                for item in items:
                    if isinstance(item, dict):
                        m_id = item.get("id") or item.get("name") or item.get("model")
                        if m_id and isinstance(m_id, str):
                            found_models.append(m_id)
                    elif isinstance(item, str):
                        found_models.append(item)

                if found_models:
                    break
            else:
                last_error = f"HTTP {resp.status_code}: {resp.text[:100]}"
        except Exception as e:
            last_error = str(e)

    if found_models:
        seen = set()
        deduped = []
        for m in found_models:
            if m not in seen:
                seen.add(m)
                deduped.append(m)
        return {
            "success": True,
            "models": deduped,
            "source": "remote",
            "message": f"成功从远程探测到 {len(deduped)} 个可用模型"
        }

    presets = PROVIDER_PRESETS.get(provider, [])
    return {
        "success": False,
        "models": presets,
        "source": "fallback",
        "error": last_error or "未探测到模型列表",
        "message": f"远程探测未响应（{last_error or '接口无返回'}），已呈现官方推荐常用模型"
    }


# -------------------------------------------------------------
# 智能体实例管理
# -------------------------------------------------------------
@router.get("/bots")
def list_bots():
    """获取所有智能体实例列表"""
    return {"bots": bot_manager.list_bots()}


@router.post("/bots/generate-preview")
def generate_bot_preview(payload: GeneratePreviewPayload):
    """根据智能体名称与目标，智能预生成人设、图标与 ID"""
    profile = bot_manager.generate_bot_profile(payload.name, payload.goal or "")
    return {"success": True, "profile": profile}


@router.post("/bots")
def create_bot(payload: CreateBotPayload):
    """新建智能体（支持仅填名称与目标，AI 自动生成人设、ID 与个性化推荐指令）"""
    target_id = payload.id
    target_avatar = payload.avatar
    target_desc = payload.description
    target_prompt = payload.base_prompt
    target_suggested_prompts = payload.suggested_prompts

    # 若未提供 ID、提示词或推荐指令，自动触发 AI 规划与生成
    if not target_id or not target_prompt or not target_avatar or not target_suggested_prompts:
        profile = bot_manager.generate_bot_profile(payload.name, payload.goal or "")
        if not target_id:
            target_id = profile["id"]
        if not target_avatar:
            target_avatar = profile["avatar"]
        if not target_desc:
            target_desc = profile["description"]
        if not target_prompt:
            target_prompt = profile["base_prompt"]
        if not target_suggested_prompts:
            target_suggested_prompts = profile.get("suggested_prompts", [])

    # 避免 ID 重名冲突
    if bot_manager.get_bot(target_id):
        target_id = f"{target_id}_{int(time.time()) % 1000}"

    bot = bot_manager.create_bot(
        bot_id=target_id,
        name=payload.name,
        avatar=target_avatar or "🤖",
        description=target_desc or "",
        base_prompt=target_prompt,
        suggested_prompts=target_suggested_prompts,
        model_override=payload.model_override
    )
    return {"success": True, "bot": _format_bot_response(bot)}


def _format_bot_response(bot):
    """格式化智能体返回体，包含记忆、技能与实时合成的系统提示词以及四层分级记忆运行期透视"""
    data = bot.model_dump()
    data["skills"] = [s.model_dump() for s in bot_manager.list_skills(bot.id)]
    data["memories"] = [m.model_dump() for m in bot_manager.list_memories(bot.id)]
    data["history_count"] = len(bot_manager.get_history(bot.id))
    data["assembled_prompt"] = agent_engine.assemble_system_prompt(bot)
    if not data.get("suggested_prompts"):
        data["suggested_prompts"] = bot_manager.infer_suggested_prompts(bot.name, bot.description, bot.base_prompt)

    # 注入四层分级记忆运行期透视全景 (Hierarchical Memory Blueprint)
    try:
        from core.agent.memory import memory_manager
        llm_cfg = bot_manager.get_llm_config()
        effective_budget = getattr(bot, "context_budget", None) or getattr(llm_cfg, "context_budget", None) or 32000
        effective_turns = getattr(bot, "max_context_turns", None) or getattr(llm_cfg, "max_context_turns", None) or 25
        history = bot_manager.get_history(bot.id, limit=500)
        saved_state = agent_engine._bot_sandbox_states.get(bot.id, {})
        uploads_dir = os.path.join(bot_manager._bot_dir(bot.id), "uploads")

        ctx = memory_manager.build_hierarchical_context(
            bot_id=bot.id,
            bot=bot,
            user_message="[实时全量透视示例：当前会话状态探查]",
            all_history=history,
            saved_state=saved_state,
            attachments=[],
            system_prompt=data["assembled_prompt"],
            manager=bot_manager,
            uploads_dir=uploads_dir,
            context_budget=effective_budget,
            max_context_turns=effective_turns
        )

        working_limit = effective_turns
        recent_slice = history[-working_limit:] if len(history) > working_limit else history
        dialogue_slice = recent_slice[:-1] if (dialogue_slice := recent_slice) and len(recent_slice) > 1 else recent_slice
        working_turns_texts = memory_manager.build_working_memory(dialogue_slice, char_budget=effective_budget)
        working_chars = sum(len(t) for t in working_turns_texts)

        data["hierarchical_memory"] = {
            "working_memory_turns": ctx.working_memory_turns,
            "working_memory_chars": working_chars,
            "context_budget": effective_budget,
            "max_context_turns": effective_turns,
            "compacted_history_count": ctx.compacted_history_count,
            "running_summary": ctx.running_summary,
            "entities_catalog": ctx.entities_catalog,
            "runtime_variables": ctx.runtime_variables,
            "total_history_count": len(history),
            "assembled_task_prompt": ctx.task_prompt
        }
    except Exception as e:
        logger.warning("Failed to build hierarchical memory preview for bot %s: %s", bot.id, str(e))
        data["hierarchical_memory"] = None

    return data


@router.get("/bots/{bot_id}")
def get_bot(bot_id: str):
    """获取智能体详情与全量资产统计"""
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="智能体不存在")
    return {"bot": _format_bot_response(bot)}


@router.post("/bots/{bot_id}/suggested-prompts")
def refresh_bot_suggested_prompts(bot_id: str):
    """根据智能体人设，通过大模型动态重新生成推荐指令（换一换）"""
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="智能体不存在")
    prompts = bot_manager.generate_bot_suggested_prompts(bot_id, force_refresh=True)
    return {"success": True, "suggested_prompts": prompts}


@router.put("/bots/{bot_id}")
def update_bot(bot_id: str, payload: UpdateBotPayload):
    """更新智能体设定"""
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="智能体不存在")

    # 内置智能体保护：禁止修改基础人设、名称、头像与描述
    if bot_id == "default_assistant":
        if payload.base_prompt is not None and payload.base_prompt != bot.base_prompt:
            raise HTTPException(status_code=400, detail="内置智能体基础设定受系统保护，不可修改")
        if payload.name is not None and payload.name != bot.name:
            raise HTTPException(status_code=400, detail="内置智能体名称受系统保护，不可修改")
        if payload.avatar is not None and payload.avatar != bot.avatar:
            raise HTTPException(status_code=400, detail="内置智能体头像受系统保护，不可修改")
        if payload.description is not None and payload.description != bot.description:
            raise HTTPException(status_code=400, detail="内置智能体描述受系统保护，不可修改")

    if payload.name is not None:
        bot.name = payload.name
    if payload.avatar is not None:
        bot.avatar = payload.avatar
    if payload.description is not None:
        bot.description = payload.description
    if payload.base_prompt is not None:
        bot.base_prompt = payload.base_prompt
    if payload.model_override is not None:
        bot.model_override = payload.model_override
    bot_manager.save_bot(bot)
    return {"success": True, "bot": _format_bot_response(bot)}


@router.delete("/bots/{bot_id}")
def delete_bot(bot_id: str):
    """删除智能体并彻底清理相关数据与运行任务"""
    if bot_id == "default_assistant":
        raise HTTPException(status_code=400, detail="默认智能体受系统内置保护，无法删除")
    agent_run_manager.stop_run(bot_id)
    ok = bot_manager.delete_bot(bot_id)
    if not ok:
        raise HTTPException(status_code=404, detail="智能体不存在")
    return {"success": True}


# -------------------------------------------------------------
# 会话与 SSE 流式对话
# -------------------------------------------------------------
@router.get("/bots/{bot_id}/history")
def get_bot_history(
    bot_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    before_id: Optional[str] = Query(default=None)
):
    """获取会话历史（支持根据 before_id 向上分页加载更早历史会话）"""
    return bot_manager.get_history_paged(bot_id, limit=limit, before_id=before_id)


@router.delete("/bots/{bot_id}/history")
def clear_bot_history(bot_id: str, delete_files: bool = Query(default=True)):
    """清空会话历史并同步清理物理磁盘上的上传附件"""
    bot_manager.clear_history(bot_id, delete_files=delete_files)
    return {"success": True}


@router.post("/bots/{bot_id}/files/cleanup")
def cleanup_bot_files(bot_id: str):
    """主动扫描并清理未在历史消息中引用的孤儿文件"""
    deleted = bot_manager.cleanup_unreferenced_files(bot_id)
    return {"success": True, "cleaned_count": deleted}


def _stream_run_sse(run: AgentRun):
    """为指定 AgentRun 构造线程安全的 SSE 流式响应，支持断线重连与全量快照同步"""
    async def sse_event_generator():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_EVENTS_PER_RUN)
        sub, snapshot = run.register_subscriber(queue, loop)
        try:
            # 1. 立即下发当前全量快照，供前端初始化或断线同步
            sync_payload = json.dumps({"run": snapshot}, ensure_ascii=False)
            yield f"event: sync\ndata: {sync_payload}\n\n"

            # 如果任务在连接建立前已完成，则直接下发终止信号后退出
            if snapshot.get("status") in ("done", "error", "stopped"):
                evt_name = "error" if snapshot.get("status") == "error" else "done"
                yield f"event: {evt_name}\ndata: {sync_payload}\n\n"
                return

            # 2. 持续等待新事件产生并流式传输
            while True:
                try:
                    event_obj = await asyncio.wait_for(queue.get(), timeout=1.0)
                    evt_name = event_obj.get("event", "message")
                    data_str = json.dumps(event_obj.get("data", {}), ensure_ascii=False)
                    yield f"event: {evt_name}\ndata: {data_str}\n\n"
                    if evt_name in ("done", "error") or run.status in ("done", "error", "stopped"):
                        break
                except TimeoutError:
                    if run.status in ("done", "error", "stopped"):
                        break
                    # 心跳保活
                    yield f": ping\n\n"
        finally:
            run.unregister_subscriber(sub)

    return StreamingResponse(
        sse_event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.get("/active-runs")
def list_all_active_runs():
    """批量查询所有智能体当前活跃运行状态（供前端多智能体列表状态指示使用）"""
    return {"active_runs": agent_run_manager.list_active_runs()}


@router.post("/bots/{bot_id}/upload")
async def upload_bot_file(
    bot_id: str,
    file: UploadFile = File(...)
):
    """
    上传文件/文档/图片附件给智能体处理
    支持 DOCX, XLSX/XLS, CSV, TXT, JSON, MD, LOG, PDF, 以及各类图片
    自动提取结构化数据摘要供大模型理解
    """
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="BOT 不存在")

    bot_dir = bot_manager._bot_dir(bot_id)
    uploads_dir = os.path.join(bot_dir, "uploads")
    try:
        os.makedirs(uploads_dir, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="无法创建文件上传目录") from exc

    original_filename = file.filename or "uploaded_file"
    safe_name = os.path.basename(original_filename)
    safe_name = re.sub(r"[^\w\.\-\u4e00-\u9fa5]", "_", safe_name)
    file_id = f"f_{int(time.time() * 1000)}"
    saved_name = f"{file_id}_{safe_name}"
    saved_path = os.path.abspath(os.path.join(uploads_dir, saved_name))

    content = await file.read(MAX_AGENT_UPLOAD_BYTES + 1)
    if len(content) > MAX_AGENT_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="上传文件超过 50 MiB 限制")
    try:
        with open(saved_path, "wb") as f:
            f.write(content)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="文件保存失败") from exc

    try:
        parsed = await run_in_threadpool(parse_file_summary, saved_path, safe_name)
    except Exception as exc:
        with suppress(OSError):
            os.remove(saved_path)
        raise HTTPException(status_code=422, detail="文件解析失败") from exc
    file_url = f"/api/agent/bots/{bot_id}/files/{saved_name}"

    data_url = None
    if parsed["category"] == "image" and len(content) <= 3 * 1024 * 1024:
        mime = file.content_type or f"image/{parsed['type']}"
        b64 = base64.b64encode(content).decode("utf-8")
        data_url = f"data:{mime};base64,{b64}"

    return {
        "success": True,
        "file": {
            "id": file_id,
            "name": safe_name,
            "saved_name": saved_name,
            "size": len(content),
            "size_formatted": parsed["size_formatted"],
            "type": parsed["type"],
            "category": parsed["category"],
            "path": saved_path,
            "url": file_url,
            "data_url": data_url,
            "summary": parsed["summary"],
            "preview": parsed["preview"],
            "meta": parsed.get("meta", {})
        }
    }


@router.api_route("/files/{bot_id}/{filename}", methods=["GET", "HEAD"])
@router.api_route("/bots/{bot_id}/files/{filename}", methods=["GET", "HEAD"])
def get_bot_file(bot_id: str, filename: str, download: bool = Query(default=False)):
    """在线预览或下载智能体已上传的文件或生成的文件（支持原文件名或前缀哈希名双向匹配）"""
    bot_dir = bot_manager._bot_dir(bot_id)
    uploads_dir = os.path.join(bot_dir, "uploads")
    safe_filename = os.path.basename(filename)
    file_path = os.path.abspath(os.path.join(uploads_dir, safe_filename))

    # 1. 尝试直接路径匹配
    if not (file_path.startswith(os.path.abspath(bot_dir)) and os.path.exists(file_path) and os.path.isfile(file_path)):
        candidate = None
        # 2. 优先从 history.json 中检索关联关系（避免同名不同批次文件混淆）
        hist_path = os.path.join(bot_dir, "history.json")
        if os.path.exists(hist_path):
            try:
                with open(hist_path, "r", encoding="utf-8") as f:
                    history = json.load(f)
                    for msg in history:
                        for att in msg.get("metadata", {}).get("attachments", []):
                            u = att.get("url", "")
                            sn = att.get("saved_name", "")
                            # 匹配 url 后缀或者原始文件名
                            if (u.endswith(f"/{safe_filename}") or att.get("name") == safe_filename) and sn:
                                sp = os.path.join(uploads_dir, sn)
                                if os.path.isfile(sp):
                                    candidate = sp
                                    break
                        if candidate:
                            break
            except Exception:
                pass

        # 3. 尝试模糊/后缀回退匹配 (如传入 screenshot.png 匹配到 f_xxx_screenshot.png)
        if not candidate and os.path.exists(uploads_dir):
            matches = []
            try:
                directory_entries = os.listdir(uploads_dir)
            except OSError:
                directory_entries = []
            for fn in directory_entries:

                if fn == safe_filename or fn.endswith(f"_{safe_filename}"):
                    p = os.path.join(uploads_dir, fn)
                    if os.path.isfile(p):
                        matches.append((p, os.path.getmtime(p)))
            if matches:
                matches.sort(key=lambda x: x[1], reverse=True)
                candidate = matches[0][0]

        if candidate and os.path.exists(candidate):
            file_path = candidate
        else:
            raise HTTPException(status_code=404, detail="文件不存在或已被移除")

    mime_type, _ = mimetypes.guess_type(file_path)
    # 图片、PDF、纯文本等格式支持浏览器直接预览
    is_previewable = bool(
        mime_type and (
            mime_type.startswith("image/")
            or mime_type in ("application/pdf", "text/plain", "text/markdown", "text/html", "application/json")
        )
    )
    disposition = "attachment" if download else ("inline" if is_previewable else "attachment")

    return FileResponse(
        file_path,
        media_type=mime_type or "application/octet-stream",
        content_disposition_type=disposition,
        filename=safe_filename
    )


@router.get("/bots/{bot_id}/files")
def list_bot_files(bot_id: str):
    """产出物与文件管理中心：获取智能体所有上传与生成的文件清单"""
    return {"files": bot_manager.list_bot_files(bot_id)}


@router.delete("/bots/{bot_id}/files/{filename}")
def delete_bot_file(bot_id: str, filename: str):
    """删除智能体文件中心里的单个文件"""
    ok = bot_manager.delete_bot_file(bot_id, filename)
    if not ok:
        raise HTTPException(status_code=404, detail="文件不存在或删除失败")
    return {"success": True}


def _unique_archive_name(clean_name: str, used_names: set[str]) -> str:
    base, ext = os.path.splitext(clean_name)
    arc_name = clean_name
    suffix = 1
    while arc_name in used_names:
        arc_name = f"{base} ({suffix}){ext}"
        suffix += 1
    used_names.add(arc_name)
    return arc_name


@router.get("/bots/{bot_id}/files-export")
def export_all_bot_files(bot_id: str):
    """一键打包下载智能体所有产出物与文件（ZIP 压缩流）"""
    import io
    import zipfile
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="BOT 不存在")

    bot_dir = bot_manager._bot_dir(bot_id)
    uploads_dir = os.path.join(bot_dir, "uploads")
    try:
        upload_names = os.listdir(uploads_dir) if os.path.exists(uploads_dir) else []
    except OSError as exc:
        raise HTTPException(status_code=500, detail="读取文件目录失败") from exc
    if not upload_names:
        raise HTTPException(status_code=404, detail="该智能体当前暂无任何产出物或文件")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        used_names = set()
        for fn in sorted(upload_names):
            fp = os.path.join(uploads_dir, fn)
            if os.path.isfile(fp):
                clean_name = re.sub(r"^f_\d+_", "", fn)
                arc_name = _unique_archive_name(clean_name, used_names)
                zf.write(fp, arcname=arc_name)
    buf.seek(0)

    import urllib.parse
    ascii_filename = f"{bot_id}_files_{int(time.time())}.zip"
    encoded_filename = urllib.parse.quote(f"{bot.name}_files_{int(time.time())}.zip")
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{ascii_filename}"; filename*=utf-8\'\'{encoded_filename}'
        }
    )


@router.get("/bots/{bot_id}/active-run")
def get_bot_active_run(bot_id: str):
    """查询指定智能体当前是否有正在进行的会话任务（用于页面刷新状态恢复）"""
    run = agent_run_manager.get_active_run(bot_id)
    if not run:
        return {"has_active_run": False, "run": None}
    return {"has_active_run": True, "run": run.to_dict()}


@router.post("/bots/{bot_id}/chat")
async def chat_with_bot_stream(bot_id: str, payload: ChatPayload):
    """
    与指定 BOT 进行流式对话 (后台解耦执行，支持断线恢复与刷新重连)
    """
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="BOT 不存在")

    # 启动/复用后台执行任务
    raw_attachments = [a.model_dump() for a in payload.attachments] if payload.attachments else None
    run = agent_run_manager.start_run(
        bot_id=bot_id,
        user_message=payload.message,
        engine=agent_engine,
        bot_manager=bot_manager,
        client_msg_id=payload.client_msg_id,
        attachments=raw_attachments
    )
    return _stream_run_sse(run)


@router.post("/bots/{bot_id}/regenerate")
async def regenerate_bot_stream(bot_id: str):
    """
    重新生成最后一条助手回复：
    弹出上一次助手回答，复用上一条用户 Prompt 及附件重新发起推理执行流
    """
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="BOT 不存在")

    last_user = bot_manager.get_last_user_message(bot_id)
    if not last_user:
        raise HTTPException(status_code=400, detail="当前暂无用户历史消息，无法重新生成")

    # 弹出上一次的助手回答
    bot_manager.pop_last_assistant_message(bot_id)

    attachments = last_user.get("metadata", {}).get("attachments")
    run = agent_run_manager.start_run(
        bot_id=bot_id,
        user_message=last_user.get("content", ""),
        engine=agent_engine,
        bot_manager=bot_manager,
        attachments=attachments,
        skip_user_append=True
    )
    return _stream_run_sse(run)


@router.post("/bots/{bot_id}/truncate-history")
def truncate_bot_history(bot_id: str, payload: TruncateHistoryPayload):
    """截断会话历史至指定消息 ID（保留该消息及之前内容，用于编辑重发前的回滚）"""
    ok = bot_manager.truncate_history(bot_id, payload.message_id)
    if not ok:
        raise HTTPException(status_code=404, detail="未找到指定的消息 ID")
    return {"success": True}


@router.get("/bots/{bot_id}/chat/stream")
async def resume_chat_stream(bot_id: str):
    """断线恢复流：重连当前正在进行中的会话流"""
    run = agent_run_manager.get_active_run(bot_id)
    if not run:
        raise HTTPException(status_code=404, detail="当前无进行中的会话任务")
    return _stream_run_sse(run)


@router.post("/bots/{bot_id}/stop")
def stop_bot_chat(bot_id: str):
    """中断当前正在执行的会话任务"""
    stopped = agent_run_manager.stop_run(bot_id)
    return {"success": stopped}


@router.post("/bots/{bot_id}/approvals/{approval_id}")
def resolve_bot_approval(bot_id: str, approval_id: str, payload: ApprovalDecisionPayload):
    """为指定智能体高危破坏性操作提交人机协同授权决策 (approve 或 reject)"""
    ok = agent_run_manager.resolve_approval(bot_id, approval_id, payload.decision)
    if not ok:
        raise HTTPException(status_code=404, detail="未找到对应的待审批高危操作或已超时关闭")
    return {"success": True, "approval_id": approval_id, "decision": payload.decision}


@router.get("/bots/{bot_id}/export")
def export_bot_chat(
    bot_id: str,
    format: str = Query(default="markdown"),
    preview: bool = Query(default=False)
):
    """一键导出当前智能体会话排障记录（支持 Markdown 与自包含单页交互式 HTML）"""
    import io
    import re
    import html as html_lib
    import urllib.parse

    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="智能体不存在")

    history = bot_manager.get_history(bot_id, limit=2000)
    if not history:
        raise HTTPException(status_code=400, detail="当前暂无会话排障记录可供导出")

    export_time = time.strftime("%Y-%m-%d %H:%M:%S")
    clean_bot_name = re.sub(r'[/\\:*?"<>|]', '_', bot.name)

    if format.lower() == "html":
        # 生成高保真、独立可离线查看的 HTML 排障报告
        turns_html = []
        for idx, msg in enumerate(history, 1):
            role = msg.get("role", "user")
            content = (msg.get("content") or "").strip()
            meta = msg.get("metadata", {}) or {}
            ts = msg.get("timestamp", "")

            if role == "user":
                atts_html = ""
                atts = meta.get("attachments", []) or []
                if atts:
                    items = "".join([
                        f"<li style='margin: 4px 0;'><span style='color: #0284c7;'>📎</span> <code>{html_lib.escape(str(a.get('name', '')))}</code> ({html_lib.escape(str(a.get('type', '')))}, {html_lib.escape(str(a.get('size_formatted', '')))})</li>"
                        for a in atts if isinstance(a, dict)
                    ])
                    atts_html = f"<div style='margin-top: 10px; font-size: 12px; color: #64748b;'><strong>附件:</strong><ul style='padding-left: 20px; margin: 4px 0;'>{items}</ul></div>"

                turns_html.append(f"""
                <div style="margin-bottom: 24px; padding: 18px 22px; border-radius: 12px; background: #f8fafc; border: 1px solid #e2e8f0;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                        <span style="font-weight: 700; color: #0f172a; font-size: 14px;">👤 用户需求 [#{idx}]</span>
                        <span style="font-size: 11px; color: #94a3b8; font-family: monospace;">{html_lib.escape(str(ts))}</span>
                    </div>
                    <div class="markdown-content" style="font-size: 14px; line-height: 1.6; color: #334155; white-space: pre-wrap;">{html_lib.escape(content)}</div>
                    {atts_html}
                </div>
                """)
            else:
                thought = meta.get("thought", "") or ""
                thought_section = ""
                if thought:
                    thought_section = f"""
                    <details style="margin-bottom: 14px; background: #f1f5f9; padding: 10px 14px; border-radius: 8px; font-size: 12px; color: #475569; border-left: 3px solid #0284c7;">
                        <summary style="cursor: pointer; font-weight: 600; color: #0369a1;">💭 推理思考过程 (Thinking)</summary>
                        <div style="margin-top: 8px; white-space: pre-wrap; font-family: monospace; line-height: 1.5; font-size: 11px;">{html_lib.escape(str(thought))}</div>
                    </details>
                    """

                code_blocks = meta.get("code_blocks", []) or []
                logs = meta.get("execution_logs", []) or []
                code_section = ""
                if code_blocks:
                    c_parts = []
                    for cb in code_blocks:
                        if not isinstance(cb, dict):
                            continue
                        step_num = cb.get("step", 1)
                        c_code = cb.get("code", "") or ""
                        matching_log = next((l for l in logs if isinstance(l, dict) and l.get("step") == step_num), None)
                        log_out = matching_log.get("output", "") if matching_log else ""
                        c_parts.append(f"""
                        <div style="margin-top: 10px; border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden;">
                            <div style="background: #0f172a; color: #94a3b8; font-size: 11px; font-family: monospace; padding: 6px 12px; display: flex; justify-content: space-between;">
                                <span>⚡ 沙箱 Python 执行 · 步骤 {step_num}</span>
                            </div>
                            <pre style="margin: 0; padding: 12px; background: #1e293b; color: #f8fafc; font-size: 12px; overflow-x: auto; font-family: monospace; line-height: 1.4;">{html_lib.escape(str(c_code))}</pre>
                            {f'<div style="background: #f8fafc; padding: 10px 12px; font-size: 11px; border-top: 1px solid #e2e8f0; font-family: monospace; color: #334155;"><strong style="color: #0284c7;">执行结果:</strong><div style="white-space: pre-wrap; margin-top: 4px;">{html_lib.escape(str(log_out))}</div></div>' if log_out else ''}
                        </div>
                        """)
                    code_section = "".join(c_parts)

                turns_html.append(f"""
                <div style="margin-bottom: 28px; padding: 20px 24px; border-radius: 12px; background: #ffffff; border: 1px solid #cbd5e1; box-shadow: 0 1px 4px rgba(0,0,0,0.05);">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid #f1f5f9;">
                        <span style="font-weight: 700; color: #0284c7; font-size: 14px;">🤖 {html_lib.escape(bot.name)} 反馈 [#{idx}]</span>
                        <span style="font-size: 11px; color: #94a3b8; font-family: monospace;">{html_lib.escape(str(ts))}</span>
                    </div>
                    {thought_section}
                    {code_section}
                    <div class="markdown-content" style="margin-top: 14px; font-size: 14px; line-height: 1.7; color: #1e293b; white-space: pre-wrap;">{html_lib.escape(content)}</div>
                </div>
                """)

        body_content = "".join(turns_html)
        html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TIM 智能体排障分析报告 - {html_lib.escape(bot.name)}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: #f1f5f9;
            color: #0f172a;
            margin: 0;
            padding: 30px 20px;
        }}
        .container {{
            max-width: 920px;
            margin: 0 auto;
            background: #ffffff;
            border-radius: 16px;
            padding: 32px 36px;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.06);
            border: 1px solid #e2e8f0;
        }}
        .header-actions {{
            display: flex;
            gap: 8px;
            margin-top: 14px;
        }}
        .btn {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 6px 14px;
            border-radius: 8px;
            font-size: 12px;
            font-weight: 600;
            text-decoration: none;
            cursor: pointer;
            border: 1px solid #cbd5e1;
            background: #ffffff;
            color: #334155;
            transition: all 0.15s ease;
        }}
        .btn:hover {{
            background: #f8fafc;
            border-color: #94a3b8;
            color: #0f172a;
        }}
        .btn-primary {{
            background: #0284c7;
            color: #ffffff;
            border-color: #0284c7;
        }}
        .btn-primary:hover {{
            background: #0369a1;
            border-color: #0369a1;
        }}
        .markdown-rendered h1, .markdown-rendered h2, .markdown-rendered h3 {{
            margin-top: 18px;
            margin-bottom: 8px;
            font-weight: 700;
            color: #0f172a;
        }}
        .markdown-rendered h1 {{ font-size: 18px; border-bottom: 1px solid #e2e8f0; padding-bottom: 6px; }}
        .markdown-rendered h2 {{ font-size: 16px; border-bottom: 1px solid #f1f5f9; padding-bottom: 4px; }}
        .markdown-rendered h3 {{ font-size: 14px; }}
        .markdown-rendered p {{ margin: 8px 0; line-height: 1.6; }}
        .markdown-rendered table {{
            border-collapse: collapse;
            width: 100%;
            margin: 12px 0;
            font-size: 12px;
        }}
        .markdown-rendered th, .markdown-rendered td {{
            border: 1px solid #cbd5e1;
            padding: 8px 12px;
            text-align: left;
        }}
        .markdown-rendered th {{
            background: #f8fafc;
            font-weight: 600;
            color: #334155;
        }}
        .markdown-rendered blockquote {{
            margin: 12px 0;
            padding: 8px 16px;
            border-left: 4px solid #0284c7;
            background: #f0f9ff;
            color: #0369a1;
            border-radius: 0 8px 8px 0;
        }}
        .markdown-rendered code {{
            background: #f1f5f9;
            padding: 2px 6px;
            border-radius: 4px;
            font-family: monospace;
            font-size: 12px;
            color: #0f172a;
        }}
        .markdown-rendered ul, .markdown-rendered ol {{
            padding-left: 20px;
            margin: 8px 0;
        }}
        .markdown-rendered li {{
            margin: 4px 0;
        }}
        @media print {{
            body {{ background: #fff; padding: 0; }}
            .container {{ box-shadow: none; border: none; padding: 0; max-width: 100%; }}
            .header-actions {{ display: none !important; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <header style="margin-bottom: 28px; padding-bottom: 20px; border-bottom: 2px solid #0284c7;">
            <div style="display: flex; align-items: center; justify-content: space-between;">
                <div style="display: flex; align-items: center; gap: 12px;">
                    <span style="font-size: 32px;">{html_lib.escape(bot.avatar or '🤖')}</span>
                    <div>
                        <h1 style="margin: 0; font-size: 20px; font-weight: 800; color: #0f172a;">TIM 智能体排障分析报告</h1>
                        <p style="margin: 4px 0 0; font-size: 13px; color: #64748b;">
                            智能体: <strong>{html_lib.escape(bot.name)}</strong> ({html_lib.escape(bot.id)})
                        </p>
                    </div>
                </div>
                <div style="text-align: right; font-size: 11px; color: #94a3b8; font-family: monospace;">
                    <div>生成时间: {html_lib.escape(export_time)}</div>
                    <div>会话记录: {len(history)} 条</div>
                </div>
            </div>
            <div style="margin-top: 14px; font-size: 12px; color: #475569; background: #f8fafc; padding: 10px 14px; border-radius: 8px; border: 1px solid #e2e8f0;">
                <strong>人设与定位:</strong> {html_lib.escape(bot.description or (bot.base_prompt or '')[:140])}
            </div>
            <div class="header-actions">
                <button type="button" onclick="window.print()" class="btn btn-primary">
                    🖨️ 打印报告 / 另存为 PDF
                </button>
                <a href="?format=markdown" class="btn">
                    📥 下载为 Markdown (.md)
                </a>
            </div>
        </header>

        <main>
            {body_content}
        </main>

        <footer style="margin-top: 36px; padding-top: 16px; border-top: 1px solid #e2e8f0; text-align: center; font-size: 11px; color: #94a3b8;">
            本报告由 Tencent Cloud TIM Studio 智能体全流程自动化生成 · 包含全链路代码执行与沙箱结果记录
        </footer>
    </div>

    <!-- 会话内容保持 HTML 转义后的纯文本，避免在导出报告中重新激活不可信 Markdown。 -->
</body>
</html>"""

        ascii_name = f"tim_report_{bot_id}_{int(time.time())}.html"
        encoded_name = urllib.parse.quote(f"{clean_bot_name}_排障报告_{time.strftime('%Y%m%d_%H%M%S')}.html")
        disposition_type = "inline" if preview else "attachment"
        return StreamingResponse(
            io.BytesIO(html_doc.encode("utf-8")),
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Disposition": f'{disposition_type}; filename="{ascii_name}"; filename*=utf-8\'\'{encoded_name}'
            }
        )

    # 默认 Markdown 格式导出
    lines = [
        f"# 📋 TIM 智能体排障分析报告 - {bot.name}",
        "",
        f"> **生成时间**: {export_time}  ",
        f"> **智能体**: {bot.avatar} **{bot.name}** (`{bot.id}`)  ",
        f"> **职责定位**: {bot.description or '腾讯云 IM 全能业务技术支持'}  ",
        f"> **累计会话**: 共计 {len(history)} 条排障与操作记录  ",
        "",
        "---",
        ""
    ]

    for idx, msg in enumerate(history, 1):
        role = msg.get("role", "user")
        content = (msg.get("content") or "").strip()
        meta = msg.get("metadata", {}) or {}
        ts = msg.get("timestamp", "")

        if role == "user":
            lines.append(f"### 👤 用户需求 [#{idx}] ({ts})\n")
            lines.append(f"{content}\n")
            atts = meta.get("attachments", []) or []
            if atts:
                lines.append("**📎 上传附件列表:**")
                for a in atts:
                    if isinstance(a, dict):
                        lines.append(f"- `{a.get('name')}` ({a.get('type')}, 大小: {a.get('size_formatted', '')})")
                lines.append("")
        else:
            lines.append(f"### 🤖 智能体回复 [#{idx}] ({ts})\n")
            thought = meta.get("thought", "") or ""
            if thought:
                lines.append("<details>\n<summary>💭 展开查看思考链推理过程</summary>\n")
                lines.append(thought)
                lines.append("\n</details>\n")

            code_blocks = meta.get("code_blocks", []) or []
            logs = meta.get("execution_logs", []) or []
            if code_blocks:
                lines.append("#### ⚙️ 沙箱 Python 代码执行与日志\n")
                for cb in code_blocks:
                    if isinstance(cb, dict):
                        step_num = cb.get("step", 1)
                        c_code = cb.get("code", "")
                        lines.append(f"```python\n# 步骤 {step_num}\n{c_code}\n```\n")
                        matching_log = next((l for l in logs if isinstance(l, dict) and l.get("step") == step_num), None)
                        if matching_log:
                            lines.append(f"**执行结果 (步骤 {step_num}):**\n\n{matching_log.get('output', '')}\n")

            lines.append(f"#### 🎯 智能体结论\n\n{content}\n")
            lines.append("\n---\n")

    md_content = "\n".join(lines)
    ascii_name = f"tim_report_{bot_id}_{int(time.time())}.md"
    encoded_name = urllib.parse.quote(f"{clean_bot_name}_排障报告_{time.strftime('%Y%m%d_%H%M%S')}.md")
    return StreamingResponse(
        io.BytesIO(md_content.encode("utf-8")),
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{ascii_name}"; filename*=utf-8\'\'{encoded_name}'
        }
    )


# -------------------------------------------------------------
# 长期记忆管理 (Memory)
# -------------------------------------------------------------
@router.get("/bots/{bot_id}/memory")
def list_bot_memories(bot_id: str):
    """获取指定 BOT 的长期记忆"""
    return {"memories": [m.model_dump() for m in bot_manager.list_memories(bot_id)]}


@router.post("/bots/{bot_id}/memory")
def add_or_update_memory(bot_id: str, payload: MemoryPayload):
    """保存或更新长期记忆"""
    item = bot_manager.save_memory(bot_id, payload.key, payload.content)
    return {"success": True, "memory": item.model_dump()}


@router.delete("/bots/{bot_id}/memory/{key}")
def delete_memory(bot_id: str, key: str):
    """删除长期记忆"""
    ok = bot_manager.delete_memory(bot_id, key)
    return {"success": ok}


# -------------------------------------------------------------
# 提示词自我增强管理 (Prompt Self-Enhancement)
# -------------------------------------------------------------
@router.get("/bots/{bot_id}/guidelines")
def list_bot_guidelines(bot_id: str):
    """获取指定 BOT 的提示词自我强化准则"""
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="BOT 不存在")
    return {"guidelines": [g.model_dump() for g in bot.learned_guidelines]}


@router.post("/bots/{bot_id}/guidelines")
def add_guideline(bot_id: str, payload: GuidelinePayload):
    """手动或代添加行为准则"""
    g = bot_manager.add_learned_guideline(bot_id, payload.instruction, payload.reason or "")
    if not g:
        raise HTTPException(status_code=404, detail="BOT 不存在")
    return {"success": True, "guideline": g.model_dump()}


@router.delete("/bots/{bot_id}/guidelines/{guideline_id}")
def delete_guideline(bot_id: str, guideline_id: str):
    """删除某条增强准则"""
    ok = bot_manager.remove_learned_guideline(bot_id, guideline_id)
    return {"success": ok}


# -------------------------------------------------------------
# 自编技能库管理 (Skills)
# -------------------------------------------------------------
@router.get("/bots/{bot_id}/skills")
def list_bot_skills(bot_id: str):
    """获取指定 BOT 沉淀的技能列表"""
    skills = bot_manager.list_skills(bot_id)
    return {"skills": [s.model_dump() for s in skills]}


@router.post("/bots/{bot_id}/skills")
def create_or_save_skill(bot_id: str, payload: SaveSkillPayload):
    """用户或前端一键将验证通过的脚本沉淀为专属技能"""
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="BOT 不存在")
    skill = bot_manager.save_skill(
        bot_id=bot_id,
        name=payload.name,
        description=payload.description,
        code=payload.python_code,
        input_schema=payload.input_schema
    )
    return {"success": True, "skill": skill.model_dump()}


@router.get("/bots/{bot_id}/skills/{name}")
def get_bot_skill(bot_id: str, name: str):
    """获取指定技能代码与入参"""
    skill = bot_manager.get_skill(bot_id, name)
    if not skill:
        raise HTTPException(status_code=404, detail="技能不存在")
    return {"skill": skill.model_dump()}


@router.post("/bots/{bot_id}/skills/{name}/run")
def run_bot_skill(bot_id: str, name: str, payload: RunSkillPayload):
    """手动测试运行某个技能"""
    tim_client = TimClient()
    from core.agent.tools import RunSkillTool
    # This standalone endpoint has no interactive AgentRun approval context.
    # High-risk cloud calls must therefore fail closed; normal calls remain usable.
    approved_paths = {
        path.strip().lower()
        for path in payload.approved_high_risk_paths
        if path and path.strip()
    }

    def approve_specific_high_risk_call(approval: dict) -> str:
        if not payload.confirm_high_risk:
            return "reject"
        api_path = str(approval.get("api_path", "")).strip().lower()
        return "approve" if api_path in approved_paths else "reject"

    runner = RunSkillTool(
        bot_id,
        bot_manager,
        tim_client,
        approval_requester=approve_specific_high_risk_call,
    )
    res = runner.forward(name, payload.kwargs)
    return res


@router.delete("/bots/{bot_id}/skills/{name}")
def delete_bot_skill(bot_id: str, name: str):
    """删除技能"""
    ok = bot_manager.delete_skill(bot_id, name)
    return {"success": ok}


# -------------------------------------------------------------
# 企业知识库与运维手册挂载 (Lightweight RAG)
# -------------------------------------------------------------
@router.get("/bots/{bot_id}/knowledge/docs")
def list_bot_knowledge_docs(bot_id: str):
    """获取指定智能体挂载的所有企业知识库与手册文档"""
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="BOT 不存在")
    docs = rag_manager.list_documents(bot_id)
    return {"docs": docs}


@router.post("/bots/{bot_id}/knowledge/upload")
async def upload_bot_knowledge_doc(
    bot_id: str,
    file: UploadFile = File(...)
):
    """上传并挂载业务规范文档/排障手册（支持 MD、TXT、DOCX、PDF、JSON、CSV）"""
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="BOT 不存在")

    filename = file.filename or "unknown_doc.txt"
    extension = os.path.splitext(filename)[1].lower()
    if extension == ".xls":
        raise HTTPException(status_code=422, detail="不支持旧版 .xls 格式，请另存为 .xlsx 后重新上传")
    if extension == ".xlsx":
        raise HTTPException(status_code=422, detail="知识库暂不支持 Excel 文件，请转换为 CSV 或 PDF 后上传")
    content = await file.read(MAX_KNOWLEDGE_UPLOAD_BYTES + 1)
    if len(content) > MAX_KNOWLEDGE_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="知识库文档超过 20 MiB 限制")
    if not content:
        raise HTTPException(status_code=400, detail="上传文件内容为空")

    doc_meta = await run_in_threadpool(rag_manager.add_document, bot_id, filename, content)
    return {
        "success": True,
        "message": f"文档《{filename}》已成功挂载并构建 RAG 索引（生成 {doc_meta.get('chunks_count')} 个检索切片）",
        "doc": doc_meta
    }


@router.put("/bots/{bot_id}/knowledge/docs/{doc_id}")
def update_bot_knowledge_doc(bot_id: str, doc_id: str, payload: ToggleKnowledgePayload):
    """启用或禁用某篇文档参与 RAG 语义检索"""
    ok = rag_manager.toggle_document(bot_id, doc_id, payload.enabled)
    if not ok:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"success": True}


@router.delete("/bots/{bot_id}/knowledge/docs/{doc_id}")
def delete_bot_knowledge_doc(bot_id: str, doc_id: str):
    """删除挂载的知识文档及其索引切片"""
    ok = rag_manager.delete_document(bot_id, doc_id)
    if not ok:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"success": True}


@router.post("/bots/{bot_id}/knowledge/search")
def test_search_bot_knowledge(bot_id: str, payload: SearchKnowledgePayload):
    """在线检索与召回调试：测试特定问题在知识库中的命中结果与匹配分数"""
    results = rag_manager.search(
        bot_id=bot_id,
        query=payload.query,
        top_k=payload.top_k or 4,
        min_score=payload.min_score or 0.05
    )
    return {
        "query": payload.query,
        "count": len(results),
        "results": results
    }


@router.get("/bots/{bot_id}/knowledge/docs/{doc_id}/download")
def download_knowledge_doc(bot_id: str, doc_id: str):
    """下载知识库原始文件"""
    path = rag_manager.get_document_file_path(bot_id, doc_id)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="文件不存在")
    doc = rag_manager.get_document(bot_id, doc_id)
    filename = doc.get("name") if doc else os.path.basename(path)
    return FileResponse(
        path=path,
        filename=filename,
        media_type="application/octet-stream"
    )


# -------------------------------------------------------------
# 多智能体协同网络拓扑与流转观测 (Agent Orchestration Topology)
# -------------------------------------------------------------
@router.get("/topology")
def get_orchestration_topology():
    """获取系统全局多智能体协同网络拓扑（节点、流转链路、调用频次与近期协同事件）"""
    topology = bot_manager.get_orchestration_topology()
    return topology


@router.get("/bots/{bot_id}/orchestrations")
def get_bot_orchestrations(bot_id: str, limit: int = Query(default=20, ge=1, le=100)):
    """获取与指定智能体相关的协同调用流水"""
    all_events = bot_manager.get_orchestrations(limit=200)
    related = [e for e in all_events if e.get("caller_bot_id") == bot_id or e.get("target_bot_id") == bot_id]
    return {"orchestrations": related[:limit]}

