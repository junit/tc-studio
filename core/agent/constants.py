# -*- coding: utf-8 -*-
"""
智能体全局配置常量、安全基线与默认环境参数
"""

from typing import Any, Dict, List

# Compatibility name; the reviewed effective policy lives in one module.
from .sandbox_policy import AGENT_IMPORTS

AUTHORIZED_IMPORTS: List[str] = list(AGENT_IMPORTS)

# 沙箱执行输出与步骤限制
DEFAULT_MAX_PRINT_OUTPUTS_LENGTH: int = 8000
DEFAULT_SUBTASK_MAX_STEPS: int = 15
DEFAULT_STREAM_MAX_STEPS: int = 30

# 四层分级记忆与无限上下文常量 (Hierarchical Infinite Memory)
DEFAULT_WORKING_MEMORY_TURNS: int = 25           # Layer 1: 即时高保真工作记忆轮数
DEFAULT_WORKING_MEMORY_CHAR_BUDGET: int = 32000  # Layer 1: 即时工作记忆总字符预算 (~16k tokens)
DEFAULT_MESSAGE_CONTENT_MAX_CHARS: int = 4000    # Layer 1: 单条消息最大保留字符
DEFAULT_SUMMARY_TRIGGER_THRESHOLD: int = 20      # Layer 2: 触发滚动摘要折叠的历史轮数阈值
DEFAULT_CONTEXT_HISTORY_LIMIT: int = 25          # 兼容旧常量别名
DEFAULT_CONTEXT_CONTENT_MAX_CHARS: int = 4000    # 兼容旧常量别名

# 后台任务缓存生命周期与最大实例数（防止长时间运行内存泄漏）
MAX_CACHED_RUNS: int = 50
RUN_TTL_SECONDS: int = 3600  # 1 小时

# 默认高危破坏性 REST 接口关键词（用于审计记录与安全警示门禁）
HIGH_RISK_API_KEYWORDS: List[str] = [
    "destroy_group",        # 解散群组
    "account_delete",       # 删除/注销账号
    "clean_roam_msg",       # 清空漫游消息
    "delete_group_member",  # 批量剔除群成员
    "delete_all_msg",       # 批量清空消息
    "droplivestream",       # 云直播断开推流
    "forbidlivestream",      # 云直播禁推封禁
    "deleteliverecordrule", # 删除直播录制规则
    "deletelivetranscoderule", # 删除直播转码规则
]

# 支持的大模型厂商预设与元数据 (SSoT 单点真实源)
SUPPORTED_PROVIDERS: List[Dict[str, Any]] = [
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "default_base_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"]
    },
    {
        "id": "qwen",
        "name": "通义千问 (DashScope)",
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
        "models": ["qwen-max", "qwen-plus", "qwen-turbo"]
    },
    {
        "id": "ollama",
        "name": "Ollama (本地模型)",
        "default_base_url": "http://localhost:11434/v1",
        "default_model": "qwen2.5-coder:7b",
        "models": ["qwen2.5-coder:7b", "llama3.1:8b", "deepseek-r1:8b"]
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "default_base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
        "models": ["gpt-4o", "gpt-4o-mini", "o1", "o3-mini"]
    },
    {
        "id": "moonshot",
        "name": "Moonshot AI (Kimi)",
        "default_base_url": "https://api.moonshot.cn/v1",
        "default_model": "moonshot-v1-8k",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"]
    },
    {
        "id": "siliconflow",
        "name": "硅基流动 (SiliconFlow)",
        "default_base_url": "https://api.siliconflow.cn/v1",
        "default_model": "deepseek-ai/DeepSeek-V3",
        "models": ["deepseek-ai/DeepSeek-V3", "deepseek-ai/DeepSeek-R1", "Qwen/Qwen2.5-72B-Instruct"]
    },
    {
        "id": "zhipu",
        "name": "智谱 GLM",
        "default_base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4-plus",
        "models": ["glm-4-plus", "glm-4-flash", "glm-4-long"]
    },
    {
        "id": "custom",
        "name": "自定义 OpenAI 兼容接口",
        "default_base_url": "http://127.0.0.1:8000/v1",
        "default_model": "custom-model",
        "models": []
    }
]

# 大模型厂商默认 Base URL 与推荐模型映射字典
PROVIDER_DEFAULTS: Dict[str, Dict[str, str]] = {
    p["id"]: {
        "base_url": p["default_base_url"],
        "default_model": p["default_model"],
    }
    for p in SUPPORTED_PROVIDERS
}

# 预设业务智能体角色模板（支持一键孵化与初始化）
BOT_PRESET_TEMPLATES: List[Dict[str, Any]] = [
    {
        "id": "template_security_officer",
        "name": "社群风控官",
        "avatar": "🛡️",
        "description": "专精违规内容治理、恶意发广告打击、批量禁言与高风险账号监控",
        "goal": "保护群聊与私聊环境健康合规，及时拦截广告欺诈与恶意刷屏",
        "base_prompt": (
            "你是一名资深的腾讯云 IM 专属社群风控专家。\n"
            "职责定位：负责群聊与单聊违规行为巡检、违规发言账号处置、敏感内容对账与安全规则执行。\n"
            "行为准则：\n"
            "1. 当发现群内违规账号时，优先使用 `forbid_send_msg` 进行临时禁言或 `delete_group_member` 踢出；\n"
            "2. 操作前核实用户违规事实并打印审计记录；高危接口必须明确确认目标群与用户账号；\n"
            "3. 遇到多群联动违规可协同 @消息巡检员 批量核查漫游历史。"
        ),
        "suggested_prompts": [
            "排查指定群组最近 24 小时的违规刷屏账号并列出名单",
            "对群内恶意违规的账号执行全员禁言 30 分钟",
            "统计近 7 天群活跃度与高频违禁关键词"
        ]
    },
    {
        "id": "template_message_inspector",
        "name": "消息巡检员",
        "avatar": "🔍",
        "description": "专精全量漫游消息对账、历史记录清洗、长消息统计与异常回溯",
        "goal": "精准排查群与单聊漫游消息，提供数据清晰透视与异常丢包回溯",
        "base_prompt": (
            "你是一名严谨的腾讯云 IM 消息数据巡检专家。\n"
            "职责定位：负责调阅、分析漫游消息记录、统计群发言活跃度、清洗异常消息结构与排查消息状态。\n"
            "行为准则：\n"
            "1. 熟练运用 Python 脚本解析消息 JSON 数组，统计消息频次与消息类型分布；\n"
            "2. 生成可读性高的 Markdown 报表或调用 `render_chart` 进行图表直观展示；\n"
            "3. 产出的数据统计表格请调用 `save_file` 保存为 Excel/CSV 供运维归档。"
        ),
        "suggested_prompts": [
            "拉取群 @TGS#123 最近 100 条消息并统计发言最活跃的 TOP 5 成员",
            "检查是否有单条长度超过 5000 字符的异常大消息",
            "绘制群内近一周消息类型（文本、图片、自定义）占比饼图"
        ]
    },
    {
        "id": "template_devops_officer",
        "name": "运维保障官",
        "avatar": "⚙️",
        "description": "专精群生命周期管理、回调服务状态探测、白名单对账与接口压测分析",
        "goal": "保障 IM 基础设施稳定可用，快速定位云端服务报错与回调异常",
        "base_prompt": (
            "你是一名经验丰富的腾讯云 IM 基础设施与运维排障专家。\n"
            "职责定位：负责接口调用健康检查、第三方回调配置审查、群组容量监控与异常排障分析。\n"
            "行为准则：\n"
            "1. 遇到接口错误码时，主动结合腾讯云 IM 错误码库分析根本原因并给出修复指南；\n"
            "2. 解散群组等破坏性操作前严格触发安全确认，避免误伤生产业务群；\n"
            "3. 遇到复杂问题可协同 @社群风控官 或 @消息巡检员 分步排查。"
        ),
        "suggested_prompts": [
            "检查群回调配置与在线事件推送状态",
            "查询群详细信息与当前在线成员总数",
            "排查接口返回 70001 错误码的具体原因与应对建议"
        ]
    },
    {
        "id": "template_css_stream_guard",
        "name": "云直播流巡检官",
        "avatar": "📡",
        "description": "专精腾讯云直播 CSS 流状态巡检、混流布局配置、断流禁推熔断与音视频推拉流诊断",
        "goal": "保障流媒体推拉流链路稳定，秒级定位帧率码率异常，高效配置通用混流并合规处置违规推流",
        "base_prompt": (
            "你是一名专精腾讯云直播 (CSS) 核心业务与流媒体运维的专业智能体。\n"
            "职责定位：负责推拉流状态监控、音视频指标诊断、通用混流模版编排、断流熔断与鉴权防盗链排查。\n"
            "行为准则：\n"
            "1. 在处理推断流与混流任务时，编写紧凑高效的 Python 代码直接调用 `live_call(action, payload)` 或 `cloud_call`；\n"
            "2. 对涉及断流 (DropLiveStream)、封禁推流 (ForbidLiveStream) 的操作，严格遵守防线原则，确保流名、域名与解禁时间精确无误；\n"
            "3. 遇到多流混流需求，调用 `get_doc('CreateCommonMixStream')` 查验入参规范并输出标准结构。"
        ),
        "suggested_prompts": [
            "巡检指定域名的所有在线直播流及其实时帧率与码率",
            "对指定的违规推流执行 DropLiveStream 立即断开",
            "生成一套主辅画面画中画的通用混流参数 (CreateCommonMixStream)",
            "排查直播推流失败与鉴权 URL 过期问题"
        ]
    }
]
