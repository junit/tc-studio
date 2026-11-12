# 模型运行依赖漏项与 SSE 错误处理修复

验证日期：2026-11-12。范围为当前本地开发服务与工作区代码；此前 PDF、公共 BOT 隔离和侧栏状态改动均保留。

## 用户可见故障与直接证据

用户报告同时出现 `Please install 'openai' extra to use OpenAIModel` 和“连接意外中断，请重试”。

| 检验对象 | 修复前实测 | 结论 |
|---|---|---|
| 实际 `.venv/bin/python` 中的模型初始化 | `smolagents==1.26.0` 已安装，`openai` 未安装；仅构造 `OpenAIServerModel` 即抛出相同缺包错误，没有发送模型请求 | 【确凿证实】必需扩展未声明，锁文件安装得到的环境不能调用模型 |
| 应用实际使用的 `RobustStreamingOpenAIServerModel` | 新增的真实初始化和模拟流式调用测试在修复前均以相同异常失败 | 【确凿证实】此前测试替换了模型构造，未覆盖这个依赖缺口 |
| SSE 错误事件分块 | 旧构建中将 `event: error` 与 `data:` 分两次交付，真实错误消失，页面只显示意外断流 | 【确凿证实】事件名错误地随网络读取重置 |
| 已失败任务的重连快照 | 旧构建中 `sync` 与 `error` 使用后端真实的 `{run: snapshot}` 结构，页面显示“未知异常” | 【确凿证实】前端只读取顶层 `error`，没有读取 `run.error` |
| 已收到错误后仍追加断流提示 | 代码中的终态标志在 React 状态 updater 内赋值，EOF 分支可以先于 updater 执行；与用户报告的双提示相符。同块错误用例在本次旧构建浏览器中未稳定复现双提示 | 【部分证实】存在明确的执行顺序缺口；不将未稳定复现的调度时序写为实测结论 |

根因是正式依赖漏项。上次将服务切到 `.venv` 后验证了 PDF 能力，遗漏了模型客户端初始化。
`openai` 在这里是 OpenAI 兼容协议 SDK；当前项目的 DeepSeek、Qwen、自定义地址及 Ollama 都使用这一适配器。

随后再次核对保留的旧全局 Python：同为 `smolagents==1.26.0`，它已安装 `openai==2.30.0`，但未安装 ReportLab。
旧环境的额外安装掩盖了依赖声明漏项；切换到按锁文件安装的项目 `.venv` 后，ReportLab 可用而 OpenAI SDK 缺失，
于是从 PDF 缺包变成模型初始化缺包。修复前后 smolagents 版本相同，直接证据指向解释器环境切换。

## 修复内容

- [pyproject.toml](../pyproject.toml) 和 [requirements.txt](../requirements.txt) 改为 `smolagents[openai]`，保留原版本边界。
  [uv.lock](../uv.lock)、[requirements.lock](../requirements.lock) 和 [requirements-dev.lock](../requirements-dev.lock) 同步更新。
  本次为该扩展新增 `openai==3.49.0`、`jiter==0.19.0`、`sniffio==1.3.1`；没有顺带升级既有锁定包。
- [model_runtime.py](../core/agent/model_runtime.py) 使用合成模型名、`.invalid` 地址和占位 Key 构造并关闭真实适配器。
  [app.py](../app.py) 的 lifespan 在接受请求前执行该检查，依赖缺失时拒绝启动并提示锁文件修复方式。
  检查不读取用户模型配置、不发送网络请求，仍保留中文 PDF 自检。
- [sse.ts](../web/src/lib/sse.ts) 按完整事件解析，跨网络读取保存事件名和数据，支持中文 UTF-8 拆分、CRLF、注释和多行 data。
  EOF 未完成的事件不会被认作成功结束；正常退出、提前停止和异常均释放 reader。
- [AgentPanel.tsx](../web/src/components/panels/AgentPanel.tsx) 在调度 React 更新前同步记录终态，事件名使用本次迭代的不可变值。
  运行状态更新与消息 updater 分开，文件刷新也移出 updater；失败快照读取真实错误，保留附件。
  收到明确终态后结束消费，只有没有终态且并非主动取消时才显示意外断流；失败中的代码块不会显示执行成功。
- 前端生产构建已更新至 `static/`，侧栏仍使用用户确认的 `[思考中] 我：正在分析问题……` 替换最近消息。

## 安装与运行

本地开发使用同一项目环境安装与启动：

```bash
uv sync --locked
TC_STUDIO_APP_ENV=development TC_STUDIO_AUTH_ENABLED=false uv run --locked python app.py
```

生产环境继续使用 `scripts/install_locked.sh`（内部为 `uv sync --locked --no-dev`），再以项目虚拟环境启动。
使用 pip 的部署应按 `requirements.lock` 安装，并使用同一解释器；不要仅在另一个全局 Python 中临时安装 SDK。
Docker 镜像需要按更新的锁文件重新构建后才会包含本次修复。

## 回归测试与实测结果

| 测试 | 断言目标 | 本次结果 |
|---|---|---|
| `test_real_model_adapter_initializes_without_provider_request` | 实际模型包装器可以初始化，构造阶段不发出模型请求 | 通过；修复前失败 |
| `test_real_model_adapter_streams_openai_compatible_response` | 通过真实 SDK 和模拟 HTTP transport 验证思考、正文和 token usage 的流式解析 | 通过；修复前失败 |
| `test_model_startup_check_initializes_and_closes_real_client` | 启动检查使用实际客户端并关闭资源 | 通过 |
| `test_model_startup_check_rejects_missing_openai_extra` | 缺包给出明确错误与 `uv sync --locked` 修复命令 | 通过 |
| `test_http_startup_refuses_missing_model_runtime` | 真实 FastAPI lifespan 在能力缺失时拒绝启动 | 通过 |
| `web/tests/sse.test.ts` 六项 | 每个可能的字节切分、中文、CRLF、多行、多个事件、EOF 半帧、取消与读流异常 | 6 项通过 |
| Chromium `error` / `split-error` / `sync-error` | 错误内容仅显示一次，无额外断流提示，无“未知异常”，解除生成状态 | 3 个场景通过 |
| Chromium `truncated` / `done` | 真正无终态的流保留部分答案并提示断流；正常完成保留答案且不提示断流 | 2 个场景通过 |
| 原侧栏与公共 BOT UI 回归 | 五种阶段说明替换最近消息并在结束后恢复；延迟历史/知识库不串入其他 BOT，身份变化触发重载 | 通过 |
| 全量 Python 回归 | 在独立代码、配置和数据副本运行，避免真实工作区数据被测试改写 | **415 passed in 75.12s** |
| 全新生产虚拟环境 | 临时目录执行 `uv sync --locked --no-dev`，安装 60 包、依赖检查、实际模型构造及中文 PDF 检查 | 全部通过 |
| 当前本地服务 | 自动重载后启动完成，`/healthz` 返回 200；实际 `/api/agent/config/test` 使用已有配置发送最小 `Ping` 请求 | HTTP 200，`success: true` |
| 构建与锁文件 | `pnpm build`、`uv lock --check`、`uv pip check`、`git diff --check` | 通过；保留既有大 bundle 提示 |

SSE 浏览器回归使用 `agent-browser` 技能和合成 API：不会触发真实 Agent、云 API、用户文件或聊天记录写入。
可重复运行：

```bash
pnpm --dir web test:sse
.venv/bin/python scripts/verify_public_bot_ui.py --stream-case error
.venv/bin/python scripts/verify_public_bot_ui.py --stream-case split-error
.venv/bin/python scripts/verify_public_bot_ui.py --stream-case sync-error
.venv/bin/python scripts/verify_public_bot_ui.py --stream-case truncated
.venv/bin/python scripts/verify_public_bot_ui.py --stream-case done
```

本轮真实模型验证仅为连接测试，没有重新执行 PDF 任务、调用业务云 API 或生成新用户产物。
当前验证服务为 `http://127.0.0.1:8000`；本机 `localhost` / IPv6 仍可能到另一个 Docker 服务。
没有重建或重配该运行中容器，没有进行 Linux/nsjail、真实 OIDC 或跨 worker 运行协调验收。
代码和文档均未提交或推送。
