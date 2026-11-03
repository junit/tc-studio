"""
智能体数据模型定义：BOT 实体、自增强提示词规则、长期记忆与自编技能
"""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class LearnedGuideline(BaseModel):
    """BOT 自主增强或学习到的行为准则"""

    id: str = Field(..., description="准则唯一 ID")
    instruction: str = Field(..., description="增强的具体指令内容")
    reason: str = Field(default="", description="自我强化的原因或背景说明")
    created_at: str = Field(default_factory=now_iso)


class MemoryItem(BaseModel):
    """BOT 长期事实记忆条目"""

    key: str = Field(..., description="记忆键名或标识")
    content: str = Field(..., description="记忆内容详情")
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)


class SkillItem(BaseModel):
    """BOT 自编并持久化的技能"""

    name: str = Field(..., description="技能名称（字母数字下划线）")
    description: str = Field(..., description="技能功能描述")
    code: str = Field(..., description="技能执行的 Python 脚本代码")
    input_schema: dict[str, Any] = Field(
        default_factory=dict, description="入参模式说明"
    )
    created_at: str = Field(default_factory=now_iso)


class BotStats(BaseModel):
    """BOT 运行与消耗统计数据"""

    total_runs: int = Field(default=0, description="累计对话执行次数")
    total_tokens: int = Field(default=0, description="累计消耗 Token 总量")
    total_code_executions: int = Field(
        default=0, description="累计沙箱 Python 脚本执行次数"
    )
    total_duration_seconds: float = Field(default=0.0, description="累计执行耗时 (秒)")


class BotConfig(BaseModel):
    """BOT 实体配置"""

    id: str = Field(..., description="BOT 唯一标识符")
    owner_id: str | None = Field(default=None, description="资源所有者 OIDC subject")
    visibility: str = Field(default="private", description="Bot 可见性：public 或 private")
    name: str = Field(..., description="BOT 名称")
    avatar: str = Field(default="🤖", description="头像 Emoji")
    description: str = Field(default="", description="简短功能描述")
    base_prompt: str = Field(
        default="你是一个专业的腾讯云多业务智能架构管家，具备调度腾讯云各业务全量接口（云直播 API 3.0、即时通信 IM REST 等）、编写与执行 Python 脚本、自我沉淀技能以及长期记忆的能力。",
        description="基础人设提示词",
    )
    learned_guidelines: list[LearnedGuideline] = Field(
        default_factory=list, description="自主增强的提示词准则列表"
    )
    suggested_prompts: list[str] = Field(
        default_factory=list, description="根据人格推荐的快捷提问/指令"
    )
    model_override: dict[str, Any] | None = Field(
        default=None, description="专属模型配置（如指定特殊模型），为空则沿用全局配置"
    )
    max_steps: int | None = Field(
        default=None, description="BOT 专属最大执行步数覆盖，未设置则使用模型/全局默认"
    )
    context_budget: int | None = Field(
        default=None, description="BOT 专属即时工作记忆总字符预算覆盖"
    )
    max_context_turns: int | None = Field(
        default=None, description="BOT 专属即时工作记忆最大轮数覆盖"
    )
    stats: BotStats = Field(
        default_factory=BotStats, description="累计执行与 Token 消耗统计"
    )
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)


class LLMConfig(BaseModel):
    """大模型提供商连接配置"""

    provider: str = Field(
        default="deepseek", description="提供商：deepseek, qwen, ollama, openai, custom"
    )
    model_name: str = Field(default="deepseek-chat", description="模型名称")
    base_url: str = Field(
        default="https://api.deepseek.com", description="OpenAI 兼容 Base URL"
    )
    api_key: str = Field(default="", description="API 秘钥（支持前端脱敏展示）")
    temperature: float = Field(
        default=0.2, description="采样温度（CodeAgent 推荐低温 0.1~0.3）"
    )
    max_steps: int = Field(
        default=30, description="单次任务最大执行步数（推荐 20~50 轮）"
    )
    context_budget: int = Field(
        default=32000, description="即时工作记忆总字符预算 (如 16000~128000)"
    )
    max_context_turns: int = Field(
        default=25, description="即时高保真工作区保留的最大对话轮数"
    )
