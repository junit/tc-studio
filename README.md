<div align="center">

# 🚀 TC Studio (Nova)

**腾讯云多业务云架构与自动化运维控制台 · 接口调试 · 批量运维 · 进化型 CodeAgent 智能体**

*A modern, multi-cloud developer & DevOps visual studio with Evolving CodeAgents for Tencent Cloud IM, Live (CSS) & API 3.0 Services.*

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.11-3.13](https://img.shields.io/badge/Python-3.11--3.13-brightgreen.svg)](https://www.python.org/)
[![FastAPI: 0.110+](https://img.shields.io/badge/FastAPI-0.110%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![React: 19](https://img.shields.io/badge/React-19-61DAFB.svg)](https://react.dev/)
[![TailwindCSS: v4](https://img.shields.io/badge/TailwindCSS-v4-38B2AC.svg)](https://tailwindcss.com/)
[![Multi-Cloud APIs: 427+](https://img.shields.io/badge/Multi--Cloud_APIs-427%2B_All_Supported-brightgreen.svg)](#-核心特性)
[![Tencent Cloud Live CSS: 261 APIs](https://img.shields.io/badge/API_3.0-CSS_Live_261_APIs-00a4ff.svg)](https://cloud.tencent.com/document/product/267/20455)
[![Tencent Cloud IM: 166 APIs](https://img.shields.io/badge/REST_APIs-TIM_166_APIs-ff69b4.svg)](https://cloud.tencent.com/document/product/269/1520)

<br/>

<img src="docs/images/workbench.png" alt="TC Studio 接口调试工作台" width="100%" style="border-radius: 12px; box-shadow: 0 8px 30px rgba(0,0,0,0.12);" />

</div>

---

## 📖 项目简介

**TC Studio (Nova)**（前身为 TIM Studio）是面向腾讯云多业务（即时通信 IM、云直播 CSS 及后续扩展的 API 3.0 云产品）打造的一站式现代化可视化调试、自动化批处理与**具备自我进化能力的多智能体协同运维控制台**。

在传统云产品研发与运维中，工程师常常面临多重痛点：
- **鉴权割裂**：IM 使用专有的 `UserSig` 计算，而云直播等通用业务使用复杂的 `TC3-HMAC-SHA256` 规范；
- **排障繁琐**：官方 400+ 个接口参数各异，查翻文档与构造 JSON 请求体极费工时；
- **批处理缺乏工具**：批量消息撤回、封禁推流、大群巡检往往需要临时手写一次性脚本，缺乏频率控制与审计台账；
- **高危操作缺乏保护**：解散群组、强制断流、注销账号一旦误操作，线上影响不可逆。

TC Studio 彻底告别零散脚本与繁琐鉴权，提供开箱即用的浏览器端 IDE 级管理体验：

- ⚡ **开箱即用，免配 Node 环境**：前端打包产物深度预编译内置至 `static/` 目录，**仅需 Python 3.11+ 即可一键启动**，无需安装 Node.js、pnpm 或配置前端反向代理。
- 🌐 **多业务无缝聚合与即插即用**：原生融合**即时通信 IM（166 项 REST API）**与**云直播 CSS（261 项 API 3.0）**，底层采用单点真实源（SSoT）微内核设计，支持即插即用任意扩展 TRTC、VOD、COS 等更多业务。
- 🛡️ **安全隔离与人机协同门禁 (Consent Gate)**：完全私有化本地运行，凭据与密钥不外发第三方；针对解散群组、强制断流、禁推封禁等高危破坏性操作内置**人机协同审批拦截**。
- 🤖 **进化型 CodeAgent 架构**：基于代码即动作（Code-as-Action）理念，智能体直接编写并沙箱执行 Python 脚本跨业务调度 427+ 项接口，具备动态提示词装配、准则自我进化、技能自主沉淀、企业知识库挂载与多 Agent 拓扑协同。
- 📱 **全端视口智能响应**：完美自适应宽屏显示器、笔记本分屏、平板及手机移动端（窄屏自动启用 Overlay 浮层抽屉，移动端配备微信级滑出抽屉与底栏导航）。

---

## ✨ 核心特性

### 1. 🎯 多业务全量覆盖：即时通信 IM (166) + 云直播 CSS (261) 原生驱动
- **即时通信 IM（166 项 REST API）**：
  - 涵盖单聊、群聊、全员推送、会话与资料管理、运营风控等 11 大业务板块。
  - 内置内存高速缓存版 `TLSSigAPIv2`（UserSig 生成器），自动生成鉴权 Query。
- **云直播 CSS（261 项 API 3.0）**：
  - 涵盖流状态查询、在线流监控、违规断流（`DropLiveStream`）、禁推封禁（`ForbidLiveStream`）、混流模板配置、转码录制规则等。
  - 内置 **直播防盗链推拉流地址鉴权签名器**（一键计算带 `txSecret` 与 `txTime` 的 RTMP / WebRTC / FLV / HLS 链接）。
- **多驱动通用架构**：
  - 兼容腾讯云专有体系（`UserSig` + `/v4/`）与腾讯云统一规范（`TC3-HMAC-SHA256` API 3.0 网关），支持地域接入动态路由与接入点防劫持隔离。

---

### 2. 🧪 Postman 级接口调试工作台 (Workbench)
- **多标签页与状态持久化**：支持同时打开多个跨业务接口独立调试；标签页、表单参数草稿与激活状态**自动在本地持久化记忆**，刷新页面或切换菜单不丢失上下文。
- **动态表单与专业 JSON 双向无缝转换**：支持友好**表单模式（Form）**与**专业 JSON 模式**实时双向同步，自适应嵌套对象与数组结构体。
- **TC3 签名与 cURL 终端脱机运行**：实时组装标准可执行 cURL 脚本（自动计算当前时间戳 `X-TC-Timestamp`、合规紧凑 JSON 报文签名与防重放随机数），直接粘贴至终端即可脱机运行；界面默认采用国际通用星号掩码（`...******...`）防投屏窥视，支持一键切换显隐及双向复制（「复制脱敏模板」用于安全分享，「复制可执行真实命令」用于脱机调试并附带安全合规警示）。
- **高频与最近打开智能推荐**：无打开标签页时，自动聚合展示高频常用接口与最近调试记录。
- **预览模式（Dry-Run）**：开启后仅执行入参校验与请求报文结构生成，不向腾讯云发起真实网络写入，防止生产误操作。
- **调用历史对账**：每个接口独立记录本地调用历史与完整回执，支持一键清空与回放。

---

### 3. 🚀 批量调用中心 (Runner) 与审计台账

<img src="docs/images/batch_runner.png" alt="批量任务执行中心" width="100%" style="border-radius: 8px; margin-bottom: 12px;" />

- **高频场景预设**：开箱即用内置批量撤回违规单聊消息、批量撤回群违规消息、批量禁言违规用户、批量导入账号、全员广播推送等模板。
- **表格无缝导入导出**：支持 CSV / Excel（.xlsx）批量数据拖拽解析，支持一键下载标准 CSV 模板（UTF-8 BOM 编码，Excel 双击打开无乱码）。
- **智能风控与频率限制**：系统自动结合官方文档频控规则，动态推荐与限制最小执行间隔（如 20 QPS / 5 QPS），防止触发腾讯云控制台风控封停。
- **流式终端监控**：执行过程支持实时暂停、继续、强制终止，日志流式输出并统计成功率与耗时。

<br/>

<img src="docs/images/history.png" alt="历史任务归档与审计台账" width="100%" style="border-radius: 8px;" />

- **失败重试与历史台账**：单键重试失败项，所有批次调用自动持久化归档至审计台账，支持全量与失败项独立导出。

---

### 4. 🤖 进化型 AI 智能体工作台 (CodeAgent & Multi-Agent Swarm)

<img src="docs/images/agent_workbench.png" alt="TIM 进化型 AI 智能体工作台" width="100%" style="border-radius: 8px; margin-bottom: 12px;" />

TC Studio 的智能体并非传统打字聊天的对话机器人，而是**以代码为动作语言、具备自我进化与团队协作能力的专业多云架构管家**：

- **以代码为动作语言（CodeAgent）**：
  基于 Hugging Face 开源 `smolagents` 思想架构，智能体直接编写并沙箱执行 Python 脚本来驱动腾讯云 427+ 个接口。相比传统固定 JSON Tool Calling，在多接口串接、条件分支判断与高频批量循环上具备压倒性灵活性与确定性。
- **🛡️ 高危操作人机协同门禁（Human-in-the-Loop Consent Gate）**：
  当智能体在沙箱代码或直接工具调用中触发断流（`DropLiveStream`）、禁推封禁（`ForbidLiveStream`）、解散群组（`destroy_group`）、批量注销账号（`account_delete`）等高危操作时，执行引擎即时挂起并实时流式推送至前端。UI 弹出高危授权卡片展示涉及接口与受影响目标。用户可点击【批准执行】或【拒绝拦截】，从根源杜绝模型幻觉导致的不可逆灾难。
- **动态单点真实源提示词（Zero-Hardcoding SSoT Prompt）**：
  系统提示词不再硬编码任何静态接口数，由 `DynamicTimRegistry` 运行时动态检测并注入所有已挂载云产品与最新接口统计，确保新业务接入时提示词 100% 自动对齐。
- **📖 轻量企业私有知识库挂载（RAG 检索增强）**：
  支持为各个独立智能体上传并挂载专属企业私有知识库（支持 Markdown、TXT、JSON、Word、Excel/CSV），提供轻量分块语义索引与上下文召回。
- **🕸️ 多智能体协同网络与拓扑流转视图（Multi-Agent Swarm & Visual Topology）**：
  支持通过 `@` 呼叫或直接在自然语言对话中调度跨领域智能体（如让架构管家调度直播巡检官），任务自动转派协同；提供交互式多智能体协同网络拓扑流转全景图。

<br/>

<img src="docs/images/agent_topology.png" alt="多智能体协同网络拓扑架构" width="100%" style="border-radius: 8px; margin-bottom: 12px;" />

- **📊 数据即地可视化分析（ECharts In-Chat Charting）**：
  智能体执行 Python 统计与业务分析代码时，可将群组成员分布、消息调用趋势、推流码率波动等数据就地渲染为交互式折线图、柱状图、饼图。
- **📑 会话排障记录一键导出（Chat Export）**：
  支持将复杂的排障过程、执行的 Python 代码片段、控制台 Stdout、接口回执与可视化图表，一键导出为纯净的 **Markdown (.md)** 文档或脱敏自包含 **HTML 复盘报告**。
- **🔌 模型服务一键探测与兼容**：
  支持一键动态探测后端 Base URL 下的 `/models`、`/v1/models` 以及 Ollama `/api/tags` 模型列表；全面兼容 **DeepSeek (V3/R1)**、**通义千问 (Qwen-2.5)**、**本地 Ollama** 及任何符合 OpenAI 规范的模型。

---

### 5. 📚 原生级官方文档与规范中心 (Docs Center & Common Specs)

<img src="docs/images/docs_center.png" alt="官方原生文档中心" width="100%" style="border-radius: 8px;" />

- **离线全量文档**：内置全量 427+ 项 API 结构化文档、请求出入参说明与数据结构字典，离线环境亦可极速毫秒级查阅。
- **公共规范中心**：提供云直播与 API 3.0 官方错误码字典（支持业务错误码速查排查）、地域接入节点（`ap-guangzhou` 等）与域名映射规范。
- **在线同步引擎**：支持直接在线同步腾讯云官方目录树与最新文档正文，热重载生效。
- **一键直达调试**：在文档中心查阅任意接口时，可一键将其装载到调试工作台中直接发起测试。

---

### 6. 🌐 多环境与密钥安全管理 (Settings)

<img src="docs/images/environments.png" alt="多环境配置与密钥安全管理" width="100%" style="border-radius: 8px;" />

- **多套凭据隔离**：支持配置多套环境（如 PROD 生产环境、DEV 开发沙箱、TEST 测试等），支持打标、备注与一键平滑切换。
- **IM 与 API 3.0 密钥聚合**：同时纳管 IM 的 `sdkappid`/`secret_key` 与 API 3.0 的 `tc_secret_id`/`tc_secret_key` 以及直播推拉流防盗链 Key。
- **防覆盖保护与脱敏**：前端展示时密钥自动前4后4星号脱敏；提交更新时即便携带掩码亦不会覆盖后台真实密钥。
- **一键连通性测试**：一键检测当前环境鉴权配置、网络延迟与腾讯云连通性。

---

### 7. 📱 全视口响应式设计 (Mobile & Split-Screen)

<div align="center">
  <img src="docs/images/mobile.png" alt="移动端原生底栏自适应" width="360" style="border-radius: 20px; box-shadow: 0 10px 25px rgba(0,0,0,0.15);" />
</div>

- **智能悬浮抽屉（Overlay Drawer）**：在屏幕宽度小于 1440px 或笔记本分屏下，智能体右侧资产与设置抽屉自动升级为带毛玻璃遮罩的悬浮滑出层，彻底杜绝主会话窗挤压变形与横向滚动条。
- **移动端原生级体验**：在屏幕宽度小于 768px 时自动激活原生风格的**底部导航栏**、**顶栏汉堡菜单**与**微信级侧边滑出抽屉**。

---

## 📂 项目结构

```text
.
├── apis/                             # 业务领域 API 路由模块
│   └── agent.py                      # 智能体引擎、多 BOT 调度、RAG 与门禁审批 REST/SSE 路由
├── core/                             # 核心支撑引擎 (SSoT 架构)
│   ├── agent/                        # 智能体架构实现
│   │   ├── engine.py                 # CodeAgent 沙箱执行引擎与动态提示词装配
│   │   ├── bot_manager.py            # 多 BOT 实例、通用人设准则、技能树与记忆持久化
│   │   ├── run_manager.py            # 异步运行实例与高危门禁拦截控制器
│   │   ├── rag_manager.py            # 轻量私有知识库挂载与语义检索召回管理器
│   │   ├── tools.py                  # 多云通用调用工具 (cloud_call 等) 与 Python 沙箱函数
│   │   └── constants.py              # 预设角色模版、高危 API 规则与系统提示词
│   ├── cloud_client.py               # 统一多业务调度网关 (UniversalCloudClient) 与防劫持路由
│   ├── tc3_client.py                 # 腾讯云 API 3.0 (TC3-HMAC-SHA256) 签名通信客户端
│   ├── live_util.py                  # 云直播推拉流防盗链鉴权计算工具
│   ├── common_specs_syncer.py        # API 3.0 公共规范与官方错误码同步器
│   ├── registry.py                   # 动态 API 注册中心与服务摘要汇总 (SSoT)
│   ├── tim_full_catalog.json         # 官方 IM 166 项全量接口元数据（只读发布 fallback）
│   ├── live_full_catalog.json        # 官方云直播 261 项 API 3.0 元数据（只读发布 fallback）
│   ├── live_data_types.json          # 云直播复杂数据结构体字典（只读发布 fallback）
│   ├── live_common_specs.json        # 官方错误码/地域公共规范（只读发布 fallback）
│   ├── config.py                     # 多环境配置管理、密钥脱敏与存储
│   ├── sig_util.py                   # 腾讯云 TLSSigAPIv2 (UserSig) 算法与高速内存缓存
│   ├── tim_client.py                 # 通用 IM HTTP 调用客户端
│   ├── batch_presets.py              # 批量运维预设场景模版与 QPS 风控规则
│   ├── persistence.py                # 统一 SQLite schema、迁移与持久化适配器
│   ├── file_storage.py               # 可重建 JSON 缓存的跨进程原子发布
│   ├── task_runner.py                # 批处理执行器与审计台账
│   ├── sync_task_manager.py          # 文档/目录同步任务的持久化状态机
│   ├── doc_syncer.py                 # 官方文档抓取与解析引擎
│   └── tree_syncer.py                # 官方目录树在线同步引擎
├── data/                             # Blob/文件层与旧格式迁移源
│   ├── bots/<bot>/uploads/           # 上传原件与生成文件
│   ├── bots/<bot>/knowledge/files/   # RAG 原始文档
│   ├── bots/<bot>/skills/*.py        # 技能源码
│   └── llm_config.json               # 本地模型配置（可能含密钥，需收紧权限）
├── docs/                             # 项目文档与展示资源
│   ├── images/                       # 高清脱敏界面截图 (WebP / 压缩 PNG)
│   └── TENCENTCLOUD_API3_INTEGRATION_GUIDE.md # 腾讯云 API 3.0 新业务扩展标准指导文档
├── scripts/                          # 运维工具与离线同步脚本
│   ├── sync_live_catalog.py          # 云直播全量 API 3.0 目录爬取脚本
│   ├── audit_and_sync_live_docs.py   # 云直播文档与数据结构体同步审计脚本
│   ├── sync_common_docs.py           # 官方错误码与规范同步脚本
│   └── run_withdraw.py               # 独立单聊消息批量撤回命令行工具 (CLI)
├── static/                           # 前端发布产物 (已构建单包，普通运行无需 Node.js)
│   ├── index.html
│   ├── favicon.svg
│   └── assets/                       # 编译后的极轻 JS/CSS 单包
├── storage/                          # 本地运行期存储
│   ├── tc_studio.sqlite3             # 统一 SQLite 主库（运行期生成）
│   ├── tim.sqlite3                   # 旧版主库（仅兼容发现，不与新库合并）
│   ├── batch_results/                 # 大型批处理结果正文（JSONL/Blob）
│   ├── docs/                         # 官方全量 427+ API 离线结构化文档库 (JSON)
│   ├── catalogs/                     # 运行期目录覆盖（同步任务写入，发布包目录保持只读）
│   ├── docs_cache/                   # 官方详情抓取缓存
│   ├── live_common_specs.json        # 运行期公共规范覆盖
│   ├── live_data_types.json          # 运行期结构体字典覆盖
│   └── official_live_tree.json       # 官方在线同步目录树缓存
├── tests/                            # 自动化单元测试套件 (pytest)
│   ├── test_studio.py                # 基础功能与接口工作台测试
│   ├── test_agent.py                 # 智能体、门禁、知识库与多 BOT 测试
│   ├── test_live.py                  # 云直播 API 3.0 联调与防盗链测试
│   └── test_dynamic_prompt.py        # 动态提示词自适应与 SSoT 自动化测试
├── web/                              # 前端源代码 (React 19 + Tailwind v4 + Vite + Zustand)
├── app.py                            # FastAPI 后端服务主入口
├── config.example.json               # 配置文件模板 (含 IM 与 API 3.0 多环境示例)
├── pyproject.toml                    # Python 版本与直接依赖声明
├── uv.lock                           # uv 可重复解析锁文件
├── requirements.lock                 # 生产依赖锁文件（含 hashes）
├── requirements-dev.lock             # 开发依赖锁文件（含 hashes）
├── scripts/install_locked.sh         # 锁定生产环境安装与编译检查入口
├── requirements.txt                  # 范围版本兼容入口，不用于生产发布
├── LICENSE                           # 开源许可协议 (MIT)
└── README.md
```

---

## 🚀 极速上手

### 运行环境准备
- **操作系统**：macOS / Linux / Windows
- **Python 版本**：Python 3.11、3.12 或 3.13；仓库通过 `pyproject.toml` 明确限制为 `>=3.11,<3.14`

### 1. 安装后端依赖

```bash
# 克隆仓库
git clone git@github.com:junit/tim-studio.git
cd tim-studio

# 生产依赖：从 uv.lock 安装，不重新解析版本
./scripts/install_locked.sh

# 等价的 uv 命令
uv sync --locked --no-dev

# 没有 uv 时的 pip 兼容入口
python -m pip install --require-hashes -r requirements.lock
```

`requirements.txt` 仍保留给旧的兼容安装流程，使用范围版本，不能作为生产环境的可重复构建入口。开发环境使用 `uv sync --locked`，或安装 `requirements-dev.lock`。

如果使用企业内网 PyPI 镜像，镜像必须先同步 `requirements.lock` 中的全部版本和目标平台 wheel；否则应切换到已完整同步的索引。

锁文件和双环境一致性检查：

```bash
uv lock --check
UV_PROJECT_ENVIRONMENT=/tmp/tim-lock-a uv sync --locked --no-dev
UV_PROJECT_ENVIRONMENT=/tmp/tim-lock-b uv sync --locked --no-dev
```

### 2. 准备配置文件

复制模板生成 `config.json`，并填入您的腾讯云凭据（可从 [腾讯云 IM 控制台](https://console.cloud.tencent.com/im) 及 [腾讯云访问管理控制台](https://console.cloud.tencent.com/cam/capi) 获取）：

```bash
cp config.example.json config.json
```

编辑 `config.json`（支持同时配置 IM 与 API 3.0 密钥）：
```json
{
  "active_env_id": "env_dev",
  "environments": [
    {
      "id": "env_dev",
      "name": "开发联调沙箱",
      "tag": "DEV",
      "remark": "开发联调与测试沙箱环境",
      "sdkappid": 1400000000,
      "secret_key": "YOUR_DEV_IM_SECRET_KEY",
      "admin_user": "administrator",
      "domain": "console.tim.qq.com",
      "tc_secret_id": "YOUR_DEV_TC_SECRET_ID",
      "tc_secret_key": "YOUR_DEV_TC_SECRET_KEY",
      "tc_region": "ap-guangzhou",
      "tc_endpoint": "live.tencentcloudapi.com",
      "live_push_domain": "your-push.domain.com",
      "live_play_domain": "your-play.domain.com",
      "live_push_key": "YOUR_LIVE_PUSH_KEY",
      "live_play_key": "YOUR_LIVE_PLAY_KEY",
      "delay_sec": 0.05
    }
  ]
}
```

> 💡 **提示**：您也可以先不修改 `config.json`，直接启动服务后，在网页的 **「环境配置」** 页面中直接可视化添加与测试环境！

### 3. 启动服务

```bash
# 本地开发（直接运行 app.py 时也会使用这两个安全的本地默认值）
TC_STUDIO_APP_ENV=development TC_STUDIO_AUTH_ENABLED=false python3 app.py
```

服务启动后，在浏览器中打开：
👉 **http://127.0.0.1:8000**

---

## 🤖 智能体大模型配置 (可选)

如需使用 **AI 智能体工作台**，只需在界面左侧点击 **「大模型引擎 - 配置」** 或在 `data/llm_config.json` 中配置即可：

- **DeepSeek 官方 API**：
  - 接口地址：`https://api.deepseek.com`
  - 模型标识：`deepseek-chat` 或 `deepseek-reasoner`
  - 填入您的 DeepSeek API Key。
- **本地 Ollama（100% 本地私有化，零成本）**：
  - 启动本地 Ollama（如运行 `ollama run qwen2.5:7b`）
  - 接口地址：`http://localhost:11434/v1`
  - 点击「探测模型」即可一键选定本地模型。
- **通义千问 / OpenAI / vLLM / 兼容服务**：
  - 填入对应 Base URL、API Key 与 Model Name 即可，支持点击动态探测下拉列表一键匹配。

企业局域网中的 Ollama、vLLM 或 OpenAI 兼容模型默认仍会被出站 SSRF 守卫拦截。部署方明确批准后，可用逗号分隔的精确主机名、IP 或 CIDR 配置 `TC_STUDIO_ALLOW_PRIVATE_MODEL_HOSTS`，例如 `TC_STUDIO_ALLOW_PRIVATE_MODEL_HOSTS=10.20.0.15,gpu.internal,192.168.20.0/24`；域名解析到的所有地址都必须是私有地址，通配符和 `host:port` 不受支持。Ollama 以外的端口可另用 `TC_STUDIO_LOCAL_MODEL_ALLOWED_PORTS` 配置。旧的无前缀变量仍兼容读取。

> 💡 **代理/TUN 模式免配置支持 (Out-of-the-Box)**：若本地开启了 Clash / Surge / Shadowrocket 等代理客户端的 TUN 增强模式（Fake-IP），公网域名可能会被操作系统映射为 `198.18.0.0/15` 保留网段。系统已内置主流官方大模型域名（DeepSeek、通义千问、OpenAI、Anthropic、Moonshot、Zhipu、SiliconFlow 等）在 Fake-IP 下的自动信任与放行，**无需配置任何环境变量，开箱即用**。`TC_STUDIO_ALLOW_PRIVATE_MODEL_HOSTS` 仅用于企业局域网内的自建内网服务器。

---

## 🔌 扩展接入腾讯云其它 API 3.0 业务 (TRTC / VOD 等)

TC Studio 底层采用微内核即插即用架构。如果您需要接入腾讯云其它业务（如 TRTC 实时音视频、VOD 云点播、COS 等）：

1. 请查阅官方集成指南：👉 **[`docs/TENCENTCLOUD_API3_INTEGRATION_GUIDE.md`](docs/TENCENTCLOUD_API3_INTEGRATION_GUIDE.md)**；
2. 发布包构建阶段可生成 `core/{service}_full_catalog.json`；线上同步任务将覆盖写入
   `${TC_STUDIO_STORAGE_DIR}/catalogs/{service}_full_catalog.json`，不会修改只读代码目录；
3. 系统会优先读取运行期覆盖，找不到时回退到发布包 catalog。其他进程会在下次访问
   注册中心时检测文件版本变化并自动 reload，工作台、API 树、文档中心以及智能体提示词与工具保持一致。

---

## 💻 前端二次开发与单元测试 (可选)

如果您需要修改 UI 样式或二次开发前端组件，可进入 `web/` 目录运行 Vite 开发服务器：

```bash
cd web

# 安装依赖 (推荐使用 pnpm)
pnpm install

# 启动热重载开发服务器 (默认端口 5173，自动反向代理到 8000 端口)
pnpm run dev

# 构建前端产物并自动同步至 static/ 生产目录
pnpm run build
```

运行自动化测试套件：
```bash
pytest
```

---

## 🔐 可插拔企业级认证（本地开发 / OIDC / Keycloak / Authentik）

系统提供可插拔鉴权机制，并对缺失配置 fail-closed。必须显式设置 `TC_STUDIO_APP_ENV` 与 `TC_STUDIO_AUTH_ENABLED`；本地开发可设置 `TC_STUDIO_APP_ENV=development`、`TC_STUDIO_AUTH_ENABLED=false`，生产环境必须设置 `TC_STUDIO_APP_ENV=production`、`TC_STUDIO_AUTH_ENABLED=true` 以及完整 OIDC 配置。旧的 `APP_ENV`、`AUTH_ENABLED` 和 `ALLOW_INSECURE_LOCAL_AUTH` 仍兼容读取，任何未配置环境变量或非本地环境关闭认证的请求都会被拒绝。

### 1. 环境变量配置指南

```bash
# 本地开发（必须显式声明，不会再从缺失变量推断本地管理员模式）
export TC_STUDIO_APP_ENV=development
export TC_STUDIO_AUTH_ENABLED=false

# 启用 OIDC 认证
export TC_STUDIO_APP_ENV=production
export TC_STUDIO_AUTH_ENABLED=true
export TC_STUDIO_AUTH_PROVIDER=oidc
export OIDC_ISSUER_URL=https://sso.example.com/realms/ops
# aud/client_id 是身份系统的外部契约，只有身份系统同步变更后才能改名
export OIDC_AUDIENCE=tim-studio
export OIDC_CLIENT_ID=tim-studio

# 可选：自定义 JWKS 公钥验签端点（默认自动取 Keycloak 标准端点）
export OIDC_JWKS_URL=https://sso.example.com/realms/ops/protocol/openid-connect/certs
# 旧 AUTH_PROVIDER / TIM_AUTH_PROVIDER 仍兼容读取；OIDC_* 名称保持外部身份系统契约

# 启动服务
python3 app.py
```

### 2. CodeAgent 执行边界（Linux 生产必须使用 nsjail）

主 CodeAgent 的代码执行通过独立的非 daemon worker、父子 JSON RPC、父端硬超时和进程组回收实现。单线程 POSIX 开发进程可使用 `fork`；FastAPI/AgentRun 多线程场景会自动切换为 spawn + RPC，Windows 始终使用 spawn + RPC。

Linux 生产、预发布环境必须使用 `nsjail`，并由部署提供最小 rootfs 与 seccomp-bpf（Kafel）策略文件：

```bash
export TC_STUDIO_APP_ENV=production
export TC_STUDIO_CODE_EXECUTOR_MODE=nsjail
export TC_STUDIO_NSJAIL_BIN=/usr/local/bin/nsjail
export TC_STUDIO_NSJAIL_ROOT=/opt/tc-studio/nsjail-rootfs
export TC_STUDIO_NSJAIL_PYTHON=/usr/bin/python3
export TC_STUDIO_NSJAIL_SECCOMP_POLICY=/opt/tc-studio/nsjail-python.policy
python3 app.py
```

nsjail worker 只接收工具名和 RPC 代理；云客户端、BotManager、AgentEngine 以及审批回调留在父进程。nsjail profile 应将应用代码与 Python 运行时以只读方式挂载，将 `/tmp` 设为临时文件系统，启用网络、PID/IPC/UTS 命名空间隔离，并设置 CPU、地址空间、文件描述符和墙钟时间上限。`TC_STUDIO_NSJAIL_SECCOMP_POLICY` 是宿主机上的 Kafel 策略文件路径。未安装 nsjail、未配置 rootfs 或缺少 seccomp 策略时，生产请求会 fail-closed；旧的 `TIM_NSJAIL_*` 变量仍可兼容读取。

macOS/Windows 生产环境没有本项目支持的 nsjail 部署契约。此时应显式设置 `TC_STUDIO_CODE_EXECUTOR_MODE=spawn`；历史配置 `fork` 也会被强制切换为 spawn + JSON RPC，并在启动时记录安全警告：子进程只拿工具名称和代理，真实工具调用留在父进程，超时、父死通知和进程树回收仍有效。该路径**不提供 nsjail 的操作系统级隔离**，没有 seccomp、namespace 或 rootfs 边界，不能宣称等同于沙箱；面向公网或高风险租户的生产部署应迁移到 Linux `nsjail`。旧的 `TIM_CODE_EXECUTOR_MODE` 仍可兼容读取。

### 3. RBAC 角色与权限矩阵

系统通过解码 OIDC Token claims（自动识别 `roles`、`groups`、`realm_access.roles` 或 `resource_access`）提取用户角色，并实行严格分级授权：

| 角色 / 身份 | 允许操作的范围 | 核心限制与保护策略 |
| :--- | :--- | :--- |
| **`admin` / `administrator`** (系统管理员) | • 系统与 LLM 大模型配置<br>• 多环境凭证增删改与测试<br>• 转移私有 Bot 归属<br>• 查看/复制完整未脱敏签名凭据<br>• 查看并清空所有人的批处理台账 | 具备最高控制权限，向剪贴板写入未脱敏签名时系统自动弹出安全操作警示。 |
| **`operator` / `editor`** (业务运维/开发) | • 接口单次调用与预览（Dry-Run）<br>• 发起/管理自己的批处理任务<br>• 使用 AI 智能体并持久化独立会话记忆<br>• 执行已授权的 Agent 运维技能 | **凭据自动脱敏保护**：cURL 脚本与响应中的有效签名（UserSig/TC3）由服务端自动抹除（`<redacted>`），从源头阻断权限越界与外泄。 |
| **未授权 / 匿名访问** | • 仅限静态资产及文档中心浏览 | 在 `TC_STUDIO_AUTH_ENABLED=true` 时，所有 `/api/` 路由严格返回 HTTP 401。 |

### 4. 生产部署与单点登录（SSO）接入推荐

TC Studio 前端为纯静态轻量单页应用（SPA），支持在请求头中自动携带 `Authorization: Bearer <token>`：
* **Token 注入**：前端加载时会自动读取 `sessionStorage` 中的 `tc_studio_access_token`；
  升级期间也会兼容读取旧的 `tim_access_token`，并附加到所有同源 `/api/` 请求中；
* **生产推荐架构**：TC Studio 本身不内置 OAuth 授权码重定向页面。生产环境推荐前置部署 **[oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/)**、API 网关（如 APISIX/Traefik）或企业反向代理：
  ```text
  [ 浏览器客户端 ] 
         │
         ▼
  [ oauth2-proxy / SSO 网关 ] ── (未登录跳 Keycloak 登录页，登录后注入 Bearer Token)
         │
         ▼
  [ TC Studio (FastAPI) ] ──── (OIDC JWKS 本地验签 + 角色判定 + 租户记忆隔离)
  ```

### 5. 升级与租户安全注意事项

1. **私有 Bot 资产迁移**：开启认证后，旧版本创建且 `owner_id` 为空的私有 Bot 不会自动认领给任何人（避免越权）。管理员可通过 `POST /api/agent/bots/{bot_id}/ownership` 显式分配给指定用户的 OIDC Subject。
2. **云凭证配额隔离**：当前所有云操作默认使用激活环境配置的腾讯云 API 密钥。`operator` 控制“谁有权发起操作”，如需物理级租户费用与额度隔离，需为对应租户配置独立子账号（RAM Role）。

> 💡 **架构原则**：认证解决“用户是谁”与系统级 RBAC，Bot、任务历史与知识库由系统按 `owner_id` 自动实行逻辑租户隔离。公网生产部署建议配合 HTTPS、反向代理速率限制与出站安全控制。

---

## 🔒 安全与隐私保障

1. **零第三方网络依赖**：除向腾讯云官方 API 域名（如 `console.tim.qq.com` 或 `live.tencentcloudapi.com`）发起您主动调试的请求外，TC Studio 不向任何第三方服务器发送用户数据或凭据。
2. **高危人机协同门禁**：对断流、禁推、解散群、删除账号等破坏性 API 实施沙箱拦截与人机确认，严防模型幻觉造成线上事故。
3. **凭据与签名全链路脱敏**：环境密钥与 cURL 命令中的敏感签名（UserSig / TC3 Authorization）在界面默认采用星号脱敏防截屏泄露；企业模式下非管理员在服务端自动剥离凭据，向剪贴板写入有效高权凭据时自动触发醒目警示。
4. **本地离线审计**：批量执行的明细台账、请求日志全部保存在本地 `storage/` 与 `data/` 目录中；清理历史时只删除已完成、失败或已取消的终态记录，运行中、暂停中和结果未知的任务会保留。

---

## 🧭 持久化与任务后端架构

持久化分层、SQLite 单机阶段边界、文件与数据库迁移范围，以及未来 PostgreSQL + Redis 的演进方案，见 [ADR-0001：持久化分层与任务后端演进](docs/adr/0001-persistence-and-task-backend.md)。

当前单机阶段默认使用 `storage/tc_studio.sqlite3` 作为统一 SQLite 主库，也可以通过
`TC_STUDIO_DATABASE_URL=sqlite:////var/lib/tc-studio/tc_studio.sqlite3` 指定持久化路径；
生产环境可通过 `TC_STUDIO_CONFIG_PATH=/var/lib/tc-studio/config.json` 把带凭据配置放在
受限运行期目录。旧版 `storage/batch_idempotency.sqlite3` 会自动导入主库并保留源文件。

应用自有运行配置统一使用 `TC_STUDIO_*` 命名空间。旧的 `TIM_*` 变量仍按兼容优先级读取：
新变量优先，旧变量仅在新变量缺失时生效；冲突时只记录变量名，不记录密钥值。默认位置
只有 `storage/tim.sqlite3` 时，应用会继续使用旧库并提示迁移；新旧数据库同时存在时只使用
`tc_studio.sqlite3`，不会自动合并或删除旧库。完整变量对照、数据库复制和回滚步骤见
[TC Studio 命名空间迁移说明](docs/operations/tc-studio-namespace-migration.md)。

BOT/RAG 和协同流水的结构化元数据已经 SQLite-first：BOT 定义、owner/visibility、长期记忆、会话摘要、会话消息、技能元数据、文件引用、RAG 文档、chunks、跨 BOT 协同事件都由 SQLite 保存和读取。新写入不会创建新的 `bot.json`、`memory.json`、`history.json`、`history.jsonl`、`session_summary.json`、技能 JSON、`docs.json`、`chunks.json` 或 `orchestrations.json`；旧 JSON/JSONL 只用于一次性迁移或没有 SQLite 行时的兼容回退。上传原件、RAG 原文档、技能 Python 源码和大型批处理结果仍在 Blob/文件层，SQLite 只保存归属、状态、哈希、大小、路径引用和索引。

批处理任务事实、租约、事件、幂等状态和同步任务状态也已经进入主库。单机多进程必须共享同一个本地持久化磁盘、`TC_STUDIO_CONFIG_PATH`、`TC_STUDIO_DATA_DIR` 和 SQLite 文件；多主机部署再切换 PostgreSQL，并按实时调度需求引入 Redis。

批处理状态、幂等键、SSE 回放、跨进程控制和历史清理语义见
[批处理持久化 API 契约](docs/api/batch-task-persistence-contract.md)。

单机部署、SQLite WAL/权限检查、备份恢复、旧 JSON 元数据迁移和生产验收边界见
[SQLite 单机运行与验收手册](docs/operations/sqlite-single-host-runbook.md)。

可直接使用以下脚本完成基本运维动作：

```bash
PYTHONPATH=. python scripts/check_sqlite_deployment.py --database /var/lib/tc-studio/tc_studio.sqlite3 --blob-root /var/lib/tc-studio/blobs --results-root /var/lib/tc-studio/storage/batch_results --backup-root /var/lib/tc-studio/backups --data-root /var/lib/tc-studio/data --storage-root /var/lib/tc-studio/storage --min-free-bytes 10737418240
PYTHONPATH=. python scripts/backup_sqlite.py --database /var/lib/tc-studio/tc_studio.sqlite3
PYTHONPATH=. python scripts/restore_sqlite.py /var/lib/tc-studio/backups/tc-studio-<timestamp>.sqlite3 --database /var/lib/tc-studio/tc_studio.sqlite3 --force
PYTHONPATH=. python scripts/migrate_json_metadata_to_sqlite.py --data-root /var/lib/tc-studio/data --database /var/lib/tc-studio/tc_studio.sqlite3
PYTHONPATH=. python scripts/acceptance_sqlite_multiprocess.py --work-dir /var/lib/tc-studio/acceptance/sqlite-multiprocess --timeout 60
PYTHONPATH=. python scripts/acceptance_sqlite_multiprocess.py --work-dir /var/lib/tc-studio/acceptance/sqlite-hard-faults --timeout 60 --hard-kill --file-faults --hard-kill-lease-seconds 5
PYTHONPATH=. python scripts/acceptance_http_multiprocess.py --work-dir /var/lib/tc-studio/acceptance/http-multiprocess --timeout 60
```

当前阶段不强制引入 Redis。Redis 只在多主机、多 Pod、跨实例队列或实时事件广播成为需求时加入；PostgreSQL 仍是未来事实数据库，Redis 不能替代它。

截至 2026-11-07，单机多进程 Linux 验收已覆盖 SQLite WAL/权限、并发幂等、跨进程
pause/resume/stop、真实 Worker `SIGKILL` 后的 `UNKNOWN` 保护、结果文件故障拒绝、
只读数据库拒绝写入，以及两个独立 Uvicorn 实例的 HTTP 状态可见性。详细证据见
[Linux 单机多进程验收记录](docs/operations/linux-single-host-acceptance-2026-11-07.md)；
这不包含 OIDC、真实腾讯云副作用、多主机或多 Pod 验收。

---

## 🤝 参与贡献

欢迎提交 Issue 和 Pull Request！
1. Fork 本项目
2. 创建您的特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交您的修改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送分支 (`git push origin feature/AmazingFeature`)
5. 新建 Pull Request

---

## 📄 开源许可证

本项目采用 **MIT License** 许可证，允许自由商业使用、修改与分发。详见 [LICENSE](LICENSE)。
