"""
智能体四层分级记忆与无限上下文管理器 (Hierarchical Infinite Memory Manager)
实现业界顶级单会话智能体（Grok, Muse, MemGPT）架构：

- Layer 1: Working Memory (即时高保真工作记忆)
  动态 Token/字符预算滑动窗口 + 工具执行与代码观测日志的精炼蒸馏 (Observation Distillation)
- Layer 2: Running Session Summary (会话演化滚动摘要)
  对滑出近期工作记忆窗口的长周期历史进行持续增量折叠与提炼，永不丢失宏观业务脉络
- Layer 3: Long-term Episodic & Entity Memory (长期语义与实体记忆)
  自增强准则 (Guidelines) + 长期事实 (Memories) + 业务技能 (Skills) + 企业知识库 (RAG) + 全局实体字典
- Layer 4: Execution Runtime Memory (沙箱运行时内存与变量空间)
  跨多轮 Python 交互式解释器变量常驻与主动自省反思，数据与计算结果免重复拉取
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from core.persistence import SqliteBotMetadataStore
from core.namespace import (
    CANONICAL_DATABASE_FILENAME,
    LEGACY_DATABASE_FILENAME,
    select_canonical_or_legacy_path,
)

from .constants import (
    DEFAULT_MESSAGE_CONTENT_MAX_CHARS,
    DEFAULT_WORKING_MEMORY_CHAR_BUDGET,
    DEFAULT_WORKING_MEMORY_TURNS,
)

logger = logging.getLogger(__name__)


@dataclass
class HierarchicalContext:
    """分级记忆装配完毕后的上下文对象"""
    task_prompt: str
    working_memory_turns: int
    compacted_history_count: int
    running_summary: str
    entities_catalog: dict[str, list[str]]
    runtime_variables: dict[str, str]


class HierarchicalMemoryManager:
    """四层分级记忆与无限上下文管理器"""

    def __init__(self):
        # 内存中缓存各 bot 的运行时实体目录，避免重复正则全量扫描
        self._entity_cache: dict[str, dict[str, set[str]]] = {}

    # -------------------------------------------------------------
    # Layer 4: Execution Runtime Memory (沙箱运行时内存与变量空间)
    # -------------------------------------------------------------
    def inspect_sandbox_variables(self, saved_state: dict[str, Any]) -> tuple[str, dict[str, str]]:
        """
        深度自省当前沙箱中的活跃变量，提取类型、数据形状与简要字段信息，
        供模型在下一轮对话中无缝复用已有数据，免去重复调用 API
        """
        if not saved_state:
            return "", {}

        var_summaries: dict[str, str] = {}
        lines: list[str] = []

        for name, val in saved_state.items():
            if name.startswith("_") or name in (
                "open", "render_chart", "save_file", "OUTPUT_DIR", "WORKSPACE_DIR", "final_answer"
            ):
                continue
            if callable(val) and not hasattr(val, "__code__"):
                continue

            try:
                # 针对 Pandas DataFrame
                value = cast(Any, val)
                if hasattr(value, "shape") and hasattr(value, "columns"):
                    shape_str = f"{value.shape[0]} 行 x {value.shape[1]} 列"
                    cols = list(value.columns)[:5]
                    cols_str = f", 列: {cols}{'...' if len(value.columns) > 5 else ''}"
                    desc = f"pandas.DataFrame ({shape_str}{cols_str})"
                # 针对列表
                elif isinstance(val, list):
                    l_len = len(val)
                    if l_len > 0 and isinstance(val[0], dict):
                        keys = list(val[0].keys())[:4]
                        sample = f", 字典元素键: {keys}"
                    elif l_len > 0 and isinstance(val[0], (str, int, float)):
                        sample = f", 样例: {val[:3]}"
                    else:
                        sample = ""
                    desc = f"list (长度 {l_len}{sample})"
                # 针对字典
                elif isinstance(val, dict):
                    d_len = len(val)
                    keys = list(val.keys())[:5]
                    desc = f"dict (包含 {d_len} 个键: {keys}{'...' if len(val) > 5 else ''})"
                # 针对基础数值/布尔/简短字符串
                elif isinstance(val, (int, float, bool)):
                    desc = f"{type(val).__name__} = {val}"
                elif isinstance(val, str):
                    desc = f"str (长度 {len(val)}, 预览: '{val[:30]}...')" if len(val) > 30 else f"str = '{val}'"
                else:
                    desc = f"{type(val).__name__}"

                var_summaries[name] = desc
                lines.append(f"- `{name}`: {desc}")
            except Exception as e:
                logger.debug("Failed to describe variable %s: %s", name, str(e))

        if not lines:
            return "", {}

        prompt_block = (
            "## ⚡ Python 沙箱常驻运行时变量 (Layer 4: Execution Runtime Memory)\n"
            "当前 Python 沙箱命名空间中已常驻以下前序计算生成的活跃变量，你编写的脚本可**直接读取与复用**，无需重新调用接口全量拉取：\n"
            + "\n".join(lines) + "\n"
        )
        return prompt_block, var_summaries

    # -------------------------------------------------------------
    # Layer 3: Long-term Episodic & Entity Memory (实体提取与目录构建)
    # -------------------------------------------------------------
    def extract_entities_from_messages(self, messages: list[dict[str, Any]]) -> dict[str, set[str]]:
        """从消息流中智能捕获关联的群组 ID、账号和常用接口标识"""
        group_ids: set[str] = set()
        user_ids: set[str] = set()
        apis: set[str] = set()

        group_regex = re.compile(r'(?:@TGS#\w+|LIVE_\w+|[A-Za-z][A-Za-z0-9_\-]*:\d+)')
        api_regex = re.compile(r'([a-zA-Z0-9_]+_open_http_svc/[a-zA-Z0-9_]+|[a-zA-Z0-9_]+_svc/[a-zA-Z0-9_]+)')

        for msg in messages:
            txt = msg.get("content", "") or ""
            meta = msg.get("metadata", {}) or {}

            # 抓取群 ID
            for g in group_regex.findall(txt):
                group_ids.add(g)

            # 抓取接口名
            for a in api_regex.findall(txt):
                apis.add(a)

            # 从 tool_calls 或代码中提取
            for code in meta.get("code_blocks", []):
                c_txt = code.get("code", "") if isinstance(code, dict) else str(code)
                for g in group_regex.findall(c_txt):
                    group_ids.add(g)
                for a in api_regex.findall(c_txt):
                    apis.add(a)

            for tc in meta.get("tool_calls", []):
                if isinstance(tc, dict):
                    args_str = str(tc.get("args") or tc.get("arguments") or "")
                    for g in group_regex.findall(args_str):
                        group_ids.add(g)
                    for a in api_regex.findall(args_str):
                        apis.add(a)

        return {
            "groups": group_ids,
            "apis": apis,
            "accounts": user_ids
        }

    # -------------------------------------------------------------
    # Layer 2: Running Session Summary (会话演化滚动摘要 / 递进压缩)
    # -------------------------------------------------------------
    def _summary_file_path(self, bot_dir: str) -> str:
        return os.path.join(bot_dir, "session_summary.json")

    @staticmethod
    def _metadata_store(
        bot_dir: str,
        manager: Any | None = None,
    ) -> SqliteBotMetadataStore:
        if manager is not None and hasattr(manager, "metadata_store"):
            return manager.metadata_store
        # Direct unit-level callers do not have a BotManager object.  Infer
        # the isolated database next to the runtime directory so tests and
        # offline tools never touch the application's production database.
        parent = Path(bot_dir).parent
        return SqliteBotMetadataStore(
            select_canonical_or_legacy_path(
                parent / CANONICAL_DATABASE_FILENAME,
                parent / LEGACY_DATABASE_FILENAME,
                label="会话 SQLite 主库",
            )
        )

    @staticmethod
    def _metadata_scope(
        bot_id: str,
        manager: Any | None = None,
    ) -> dict[str, str]:
        if manager is not None and hasattr(manager, "_metadata_scope"):
            return manager._metadata_scope(bot_id)
        return {"issuer": "", "tenant_id": "", "owner_subject": ""}

    def get_session_summary(
        self,
        bot_dir: str,
        *,
        bot_id: str | None = None,
        manager: Any | None = None,
    ) -> dict[str, Any] | None:
        """读取 SQLite 摘要；仅在缺少数据库行时兼容旧 JSON。"""
        if bot_id:
            store = self._metadata_store(bot_dir, manager)
            summary = store.get_summary(
                bot_id,
                **self._metadata_scope(bot_id, manager),
            )
            if summary is not None:
                return summary

        path = self._summary_file_path(bot_dir)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning("Failed to load session summary from %s: %s", path, str(e))
        return None

    def clear_session_memory(self, bot_id: str, bot_dir: str, manager: Any | None = None):
        """清空该智能体的滚动摘要和内存实体缓存"""
        self._entity_cache.pop(bot_id, None)
        store = self._metadata_store(bot_dir, manager)
        store.clear_summary(
            bot_id,
            **self._metadata_scope(bot_id, manager),
        )
        path = self._summary_file_path(bot_dir)
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

    def build_or_update_session_summary(
        self,
        bot_id: str,
        bot_dir: str,
        older_history: list[dict[str, Any]],
        manager: Any | None = None,
    ) -> str:
        """
        对滑出即时工作记忆窗口的长周期旧消息进行增量摘要压缩，保持宏观主线不丢失
        """
        if not older_history:
            existing = self.get_session_summary(
                bot_dir,
                bot_id=bot_id,
                manager=manager,
            )
            return existing.get("summary", "") if existing else ""

        existing_data = (
            self.get_session_summary(
                bot_dir,
                bot_id=bot_id,
                manager=manager,
            )
            or {}
        )
        last_compacted_id = existing_data.get("last_compacted_msg_id")

        # 找出尚未被压缩的新旧消息切片
        start_idx = 0
        if last_compacted_id:
            for idx, m in enumerate(older_history):
                if m.get("id") == last_compacted_id:
                    start_idx = idx + 1
                    break

        new_slice = older_history[start_idx:]
        if not new_slice and existing_data.get("summary"):
            return existing_data["summary"]

        # 增量提炼：提取诉求、操作要点与业务实体
        entities = self.extract_entities_from_messages(older_history)
        user_intents: list[str] = []
        assistant_conclusions: list[str] = []

        for m in older_history:
            role = m.get("role")
            content = (m.get("content") or "").strip()
            if not content:
                continue
            if role == "user":
                clean_q = re.sub(r"\s+", " ", content)[:100]
                if clean_q and clean_q not in user_intents:
                    user_intents.append(clean_q)
            elif role == "assistant":
                # 提取助手回答的第一句或核心结论
                lines = [l.strip() for l in content.split("\n") if l.strip() and not l.startswith(("#", "```", ">"))]
                if lines:
                    first_line = lines[0][:120]
                    if first_line and first_line not in assistant_conclusions:
                        assistant_conclusions.append(first_line)

        # 结构化构造累积全局摘要
        recent_intents = user_intents[-8:] if len(user_intents) > 8 else user_intents
        recent_conclusions = assistant_conclusions[-6:] if len(assistant_conclusions) > 6 else assistant_conclusions

        summary_parts: list[str] = []
        summary_parts.append(f"【会话至今已累计推进 {len(older_history)} 轮长周期演进】")

        if recent_intents:
            summary_parts.append("■ 核心业务脉络与阶段目标:")
            for ui in recent_intents:
                summary_parts.append(f"  - {ui}")

        if recent_conclusions:
            summary_parts.append("■ 阶段性执行成果与关键发现:")
            for ac in recent_conclusions:
                summary_parts.append(f"  - {ac}")

        if entities.get("groups") or entities.get("apis"):
            summary_parts.append("■ 历史重点关联资产与接口:")
            if entities.get("groups"):
                summary_parts.append(f"  - 涉案群组: {', '.join(sorted(list(entities['groups']))[:10])}")
            if entities.get("apis"):
                summary_parts.append(f"  - 核心接口: {', '.join(sorted(list(entities['apis']))[:8])}")

        full_summary_text = "\n".join(summary_parts)

        # SQLite 持久化；旧 session_summary.json 只允许作为一次性回退源。
        try:
            save_payload = {
                "last_compacted_msg_id": older_history[-1].get("id"),
                "total_compacted_count": len(older_history),
                "summary": full_summary_text,
                "entities": {k: list(v) for k, v in entities.items()},
                "updated_at": datetime.now().isoformat()
            }
            store = self._metadata_store(bot_dir, manager)
            store.upsert_summary(
                bot_id=bot_id,
                summary=save_payload,
                **self._metadata_scope(bot_id, manager),
            )
            with suppress(OSError):
                os.remove(self._summary_file_path(bot_dir))
        except Exception as e:
            logger.warning("Failed to save session summary: %s", str(e))

        return full_summary_text

    # -------------------------------------------------------------
    # Layer 1: Working Memory (即时工作记忆与观测蒸馏)
    # -------------------------------------------------------------
    def distill_assistant_turn(self, content: str, metadata: dict[str, Any], max_len: int) -> str:
        """
        对前序助手的执行进行观测蒸馏 (Observation Distillation)：
        保留核心回答，若伴随 Python 脚本或工具调用，自动生成紧凑元数据注解，
        避免大段原始日志占用宝贵的上下文预算。
        """
        trimmed_content = content[:max_len] + ("..." if len(content) > max_len else "")
        code_blocks = metadata.get("code_blocks", []) or []
        tool_calls = metadata.get("tool_calls", []) or []

        notes: list[str] = []
        if code_blocks:
            notes.append(f"执行了 {len(code_blocks)} 段 Python 脚本")
        if tool_calls:
            t_names = [str(tc.get("name")) for tc in tool_calls if isinstance(tc, dict) and tc.get("name")]

            if t_names:
                notes.append(f"调用工具: {', '.join(t_names[:3])}")

        if notes:
            return f"{trimmed_content}\n  *(本轮注记: {' | '.join(notes)})*"
        return trimmed_content

    def build_working_memory(
        self,
        recent_history: list[dict[str, Any]],
        char_budget: int = DEFAULT_WORKING_MEMORY_CHAR_BUDGET
    ) -> list[str]:
        """
        基于 Token/字符预算自适应组装即时工作记忆，优先保证最近轮次的高保真度
        """
        if not recent_history:
            return []

        formatted_turns: list[str] = []
        current_chars = 0

        # 从最近的一轮向前回溯填充
        for m in reversed(recent_history):
            role_label = "用户" if m.get("role") == "user" else "助手"
            content = m.get("content") or ""
            meta = m.get("metadata") or {}

            if m.get("role") == "assistant":
                turn_str = f"{role_label}: {self.distill_assistant_turn(content, meta, DEFAULT_MESSAGE_CONTENT_MAX_CHARS)}"
            else:
                turn_str = f"{role_label}: {content[:DEFAULT_MESSAGE_CONTENT_MAX_CHARS]}"

            turn_len = len(turn_str)
            if current_chars + turn_len > char_budget and formatted_turns:
                # 超过总字符预算，优雅截断更早的轮次
                break

            formatted_turns.append(turn_str)
            current_chars += turn_len

        # 恢复正向时序（从较旧到最新）
        formatted_turns.reverse()
        return formatted_turns

    # -------------------------------------------------------------
    # 核心统筹：组装完整的四层分级记忆与无限上下文
    # -------------------------------------------------------------
    def build_hierarchical_context(
        self,
        bot_id: str,
        bot: Any,
        user_message: str,
        all_history: list[dict[str, Any]],
        saved_state: dict[str, Any],
        attachments: list[dict[str, Any]] | None,
        system_prompt: str,
        manager: Any,
        uploads_dir: str,
        context_budget: int | None = None,
        max_context_turns: int | None = None,
    ) -> HierarchicalContext:
        """
        完整组装四层分级记忆与无限上下文提示词 (Prompt Assembly Pipeline)
        支持动态用户/模型上下文预算 (Context Budgeting)
        """
        bot_dir = manager._runtime_dir(bot_id)
        prompt_sections: list[str] = []

        effective_working_turns = (
            max_context_turns
            or getattr(bot, "max_context_turns", None)
            or DEFAULT_WORKING_MEMORY_TURNS
        )
        effective_char_budget = (
            context_budget
            or getattr(bot, "context_budget", None)
            or DEFAULT_WORKING_MEMORY_CHAR_BUDGET
        )

        # ---------------------------------------------------------
        # Layer 3: 长期语义与实体记忆 (Long-term & Guidelines & Skills)
        # ---------------------------------------------------------
        # 3.1 实体关联全景
        all_entities = self.extract_entities_from_messages(all_history)
        if all_entities.get("groups") or all_entities.get("apis"):
            ent_lines = []
            if all_entities.get("groups"):
                ent_lines.append(f"- 已知业务群组: {', '.join(sorted(list(all_entities['groups']))[:15])}")
            if all_entities.get("apis"):
                ent_lines.append(f"- 频繁调用接口: {', '.join(sorted(list(all_entities['apis']))[:8])}")
            prompt_sections.append(
                "## 🌐 会话全局已知关联实体 (Layer 3: Entity Catalog)\n"
                + "\n".join(ent_lines) + "\n"
            )

        # 3.2 长期事实记忆 (Memories)
        if "## 🧠 智能体长期事实记忆" not in system_prompt:
            memories = manager.list_memories(bot.id)
            if memories:
                max_injected = 20
                displayed_mem = memories[:max_injected]
                mem_text = "\n".join([f"- `{m.key}`: {m.content}" for m in displayed_mem])
                extra_hint = f"\n*(已常驻注入最近 {max_injected} 条核心事实，其余更早记忆可自主调用 `recall_memory(keyword)` 进行定向召回)*" if len(memories) > max_injected else ""
                prompt_sections.append(
                    f"## 🧠 智能体长期事实记忆 (Layer 3: Long-Term Memories)\n"
                    f"已记住的关键业务事实与用户偏好：\n{mem_text}{extra_hint}\n"
                )

        # 3.3 自增强行为准则 (Guidelines)
        if "## 🛡️ 智能体自我强化的行为准则" not in system_prompt and bot.learned_guidelines:
            guidelines_text = "\n".join([
                f"- **[准则 {idx+1}]**: {g.instruction}" + (f" （背景: {g.reason}）" if g.reason else "")
                for idx, g in enumerate(bot.learned_guidelines)
            ])
            prompt_sections.append(
                f"## 🛡️ 智能体自我强化的行为准则 (Layer 3: Self-Enhanced Guidelines)\n"
                f"这些是你与用户在过去的业务实践中沉淀的专属规则，你必须严格优先遵守：\n{guidelines_text}\n"
            )

        # 3.4 专属技能库 (Skills)
        if "## ⚡ 智能体已沉淀的专属技能库" not in system_prompt:
            skills = manager.list_skills(bot.id)
            if skills:
                skills_text = "\n".join([
                    f"- `{s.name}`: {s.description} (入参: {json.dumps(s.input_schema, ensure_ascii=False)})"
                    for s in skills
                ])
                prompt_sections.append(
                    f"## ⚡ 智能体已沉淀的专属技能库 (Layer 3: Available Skills)\n"
                    f"你可以直接通过 `run_skill(name, kwargs)` 运行这些技能，也可以在解决新场景后使用 `save_skill` 沉淀新技能：\n{skills_text}\n"
                )

        # 3.5 企业知识库 RAG 实时召回。公共 BOT 对话不允许把企业私有
        # 文档自动注入给不可信用户；私有 BOT 继续沿用原有召回链路。
        if getattr(bot, "visibility", "private") != "public":
            try:
                from .rag_manager import rag_manager
                matched_chunks = rag_manager.search(bot_id, user_message, top_k=3, min_score=0.15)
                if matched_chunks:
                    kb_context = rag_manager.format_chunks_for_prompt(matched_chunks)
                    prompt_sections.append(f"{kb_context}\n")
            except Exception as e:
                logger.warning("RAG knowledge recall error: %s", str(e))

        # ---------------------------------------------------------
        # Layer 2: 会话演化滚动摘要 (Running Session Summary)
        # ---------------------------------------------------------
        # 划分工作记忆窗口与超期长周期历史
        working_limit = effective_working_turns
        compacted_count = 0
        running_summary_text = ""

        if len(all_history) > working_limit:
            older_history = all_history[:-working_limit]
            compacted_count = len(older_history)
            running_summary_text = self.build_or_update_session_summary(
                bot_id,
                bot_dir,
                older_history,
                manager=manager,
            )
            if running_summary_text:
                prompt_sections.append(
                    "## 📜 全局会话演进脉络 (Layer 2: Running Session Summary - 长期无限记忆)\n"
                    f"{running_summary_text}\n"
                )

        # ---------------------------------------------------------
        # Layer 4: 沙箱运行时内存与变量空间 (Execution Runtime Memory)
        # ---------------------------------------------------------
        sandbox_block, var_summaries = self.inspect_sandbox_variables(saved_state)
        if sandbox_block:
            prompt_sections.append(sandbox_block)

        # ---------------------------------------------------------
        # Layer 1: 即时高保真工作记忆 (Working Memory)
        # ---------------------------------------------------------
        recent_slice = all_history[-working_limit:] if len(all_history) > working_limit else all_history
        # 排除最新一条（即当前这条 user_message 自身）
        dialogue_slice = recent_slice[:-1] if (dialogue_slice := recent_slice) and len(recent_slice) > 1 else []
        working_turns = self.build_working_memory(dialogue_slice, char_budget=effective_char_budget)

        if working_turns:
            prompt_sections.append(
                "## 💬 近期高保真交互记录 (Layer 1: Working Memory)\n"
                + "\n\n".join(working_turns) + "\n"
            )

        # 注入基础系统人设
        prompt_sections.append(f"{system_prompt}\n")

        # ---------------------------------------------------------
        # 当前用户需求与上传附件装配
        # ---------------------------------------------------------
        if attachments:
            from .file_parser import format_file_size
            att_lines = []
            for idx, att in enumerate(attachments, 1):
                att_name = att.get("name", "未命名文件")
                att_type = att.get("type", "file")
                size_str = att.get("size_formatted") or format_file_size(att.get("size", 0))
                att_preview = att.get("preview") or att.get("summary") or "已解析结构"
                att_lines.append(
                    f"- 📎 附件 {idx}: `{att_name}` ({att_type}, 大小: {size_str})\n"
                    f"  内容预览与数据结构:\n{att_preview}"
                )

            prompt_sections.append(
                f"【用户当前需求】: {user_message or '请分析并处理上传的文件附件。'}\n\n"
                f"【用户随消息上传了以下文件附件】:\n"
                f"{chr(10).join(att_lines)}\n\n"
                f"【附件处理操作提示】:\n"
                f"1. 你可以使用 `read_file(file_path)` 工具读取附件名对应的已上传文件；\n"
                f"2. 文件解析与访问由服务端限制在当前 BOT 的上传目录内，禁止使用绝对路径或自行打开宿主机文件；\n"
                f"3. 若涉及腾讯云 IM 业务（如导入账号名单、批量群禁言、白名单对账等），可结合 `tim_call` 进行自动化批量执行并汇报结果。"
            )
        else:
            prompt_sections.append(f"【用户当前需求】: {user_message}")

        full_task_prompt = "\n\n".join([s.strip() for s in prompt_sections if s.strip()])

        return HierarchicalContext(
            task_prompt=full_task_prompt,
            working_memory_turns=len(working_turns),
            compacted_history_count=compacted_count,
            running_summary=running_summary_text,
            entities_catalog={k: list(v) for k, v in all_entities.items()},
            runtime_variables=var_summaries
        )

    # -------------------------------------------------------------
    # 轮次完成时的自动增量归档与实体收敛
    # -------------------------------------------------------------
    def on_turn_completed(
        self,
        bot_id: str,
        bot_dir: str,
        all_history: list[dict[str, Any]],
        saved_state: dict[str, Any],
        max_context_turns: int | None = None,
        manager: Any | None = None,
    ):
        """当单轮交互完成时触发，异步或静默更新滚动摘要与实体缓存"""
        working_limit = max_context_turns or DEFAULT_WORKING_MEMORY_TURNS
        if len(all_history) > working_limit:
            older_history = all_history[:-working_limit]
            self.build_or_update_session_summary(
                bot_id,
                bot_dir,
                older_history,
                manager=manager,
            )


# 单例实例
memory_manager = HierarchicalMemoryManager()
