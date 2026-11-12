# 公共 / 通用 BOT 的定义、个人资源与隔离审查

审查日期：2026-11-12。基线为本轮开始时的工作区，保留此前尚未提交的 PDF、文件发布和个人知识库修复。
本轮确认的问题已在工作区修复；没有提交或推送。生产目标仍是单机多进程，不引入 Redis 或 PostgreSQL。

## 一、审查全景评估与证伪结论表

**以前处理过相关隔离问题，但本轮仍找到了遗漏，不能说以前修过就全部安全。**
之前修过的主链路是公共 BOT 的用户历史/记忆/文件分区、团队名单过滤，以及 RAG 底层遗漏 issuer/tenant 的问题。
本轮重新验证这些路径，同时深入到技能源码、自动学习、共享定义写权限、后台线程和浏览器延迟响应。

### “通用”和“公共”是两个维度

- **通用**：前端的业务领域分类，与 IM、直播并列。当前分类通过 service_type、ID、名称和描述推断，不决定访问权限。
- **公共**：后端 `visibility=public`，表示授权范围内的用户可以使用同一个助手定义。
- **私有**：后端 `visibility=private`，由 owner 使用，同一 issuer/tenant 内管理员也有管理与访问权限。
- 公共 BOT 的价值是复用助手能力。个人对话、偏好和资料不需要跟着共享。当前也没有匿名 BOT API。

公共 BOT 可理解为“一份公共配置 + 每位用户各自的会话与资料空间”。普通使用者不能改公共基础配置；
维护者更新公共配置后，后续使用该定义的用户会受影响。私有 BOT 的 owner 可以调整自己的基础配置。
实现复用同一条 BOT 定义并按用户分区状态，没有为每位用户复制一条 BOT，也不代表每人独立部署服务或独享云账号。

分类实现见 [AgentPanel.tsx](../web/src/components/panels/AgentPanel.tsx#L213)；访问规则见
[agent.py](../apis/agent.py#L48)。**“私有”不是对同租户管理员的端到端保密承诺。**

### 修复后的资源矩阵

| 对象 / 操作 | 公共 BOT | 私有 BOT | 存放与读取边界 |
|---|---|---|---|
| 名称、基础人设、模型覆盖、共享推荐指令 | 共享定义；仅所属管理范围可修改 | owner 或同范围管理员管理 | SQLite `bots`；API 修改和管理工具受定义权限限制 |
| 显式发布的共享行为准则 | 管理员明确调用准则管理 API 后，对该公共 BOT 用户共享 | 该 BOT 的准则 | SQLite `bot_guidelines`，按定义的管理范围读取 |
| 对话自动提炼的行为偏好 | **任何角色都只写当前用户记忆**，包括管理员 | 可沉淀为该私有 BOT 准则 | 公共 BOT 使用个人 `bot_memories`，不写共享定义 |
| 历史、摘要、长期记忆 | 当前 issuer + tenant + subject | BOT owner 范围；同范围管理员可访问 | SQLite，会话与摘要均使用同一范围 |
| 技能元数据、技能源码、技能删除与执行 | 当前用户个人技能；普通用户可以保存自己的技能 | 该私有 BOT 的技能 | SQL 完整范围 + 同范围源码目录 + 源码 SHA-256 校验 |
| 上传、正式产物与文件下载 | 当前用户 | BOT owner 范围 | SQL 文件索引 + 受控 Blob 目录；正式产物独立版本名 |
| 知识库文档、切片、检索、启停、下载 | 当前用户个人知识库 | BOT owner 范围 | SQL 完整范围 + 分区后的原文件目录 |
| `/bots/{id}/orchestrations` | 当前用户；管理员也不隐式扩大 | 当前用户发起的事件 | 管理员全范围观测单独放在 `/topology` |
| Python 跨轮变量、活动任务、审批、SSE | 完整身份键，但**仅在当前进程内** | 同样为当前执行用户的运行态 | 内存，不是 SQLite；跨 worker 恢复见第三章 |
| 云账号、模型提供商与云端业务权限 | 沿用平台配置与用户角色 | 沿用平台配置与用户角色 | **个人 BOT 资料隔离不代表每个人有独立云账号**；operator/高危确认仍生效 |

公共 BOT 的个人文件目录为 `bots/<bot>/users/<identity_hash>/...`。
`identity_hash` 来自 issuer、tenant、subject 三者；只有三者为真正本地模式的空 issuer、空 tenant、`local` 时才使用旧开发根目录。
关闭认证后，所有浏览器都是同一个 `local` 用户，不能通过换浏览器测试真实多用户隔离。

### 实际使用时怎么区分

- 想让多人使用同一种助手能力：使用公共 BOT。管理员维护共享人设；Alice 和 Bob 各自聊天、上传资料、保存技能，彼此的个人资源不跟着共享。
- 想只为自己调整基础人设、推荐指令和专属规则：创建私有 BOT。不要通过修改公共 BOT 的基础人设保存个人需求；公共 BOT 中的日常偏好由个人记忆承接。
- 管理员在公共 BOT 中上传文档，也只进入该管理员自己的知识库。当前没有“管理员上传后全员自动可检索”的公共知识库模式；未来如需要，必须另建显式共享资源和授权规则。
- 最终送给模型的上下文包含共享定义与当前用户可见的个人信息。个人历史、摘要、记忆、检索结果和可协同 BOT 名单各有范围检查，不能只检查基础 Prompt 后就断言整个上下文安全。
- 本地开发关闭认证时，`/api/auth/me` 的身份是 `local`，所有浏览器共享该开发身份。私有旧 BOT 的所有权认领可使用 `local`；公共 BOT 的旧状态分配是另一条管理流程，不能把认领私有 BOT 当成公共资料的自动分配。

公共 BOT **可以由多个用户同时使用**。当前活动任务键包含 BOT + issuer + tenant + subject；只有同一完整身份
在同一 BOT 再发任务时才请求停止自己的旧任务。关闭认证的两个浏览器都叫 `local`，因此不构成两个独立用户。
这条单进程内并发规则已实测，跨 Uvicorn worker 的协调仍有下文列出的限制；详见
[后续并发与运行环境复查](PDF_RUNTIME_AND_BOT_STATUS_FOLLOWUP.md)。

### 八维检查结果

| 风险维度 | 审查假设 / 潜在风险 | 检验方法与实测结论 | 严重级别 | 处置建议与状态 |
|---|---|---|---|---|
| 1. 沙箱与凭据边界 | worker 会丢失用户身份，或通过协同工具读另一用户私有 BOT | 【证伪为假警报】真实 spawn/RPC 调用个人技能及偏好工具；双用户后台线程并发检索/组装提示词；工具与 engine 两处阻断无权协同 | — | 当前测试通过；未把本地 RPC 验证当作 Linux/nsjail 主机验收 |
| 2. 鉴权与治理 | 全局公共 BOT 的“可使用”权限同时被当成“可修改”；管理员对话把个人内容写入共享准则 | 【确凿证实】租户管理员修改全局公共模板返回 200；管理员自动学习后的偏好出现在 Bob 的提示词。普通用户直接改共享准则原本已被拒绝 | P1 | 精确区分访问与定义管理范围；自动偏好改为个人记忆；已修复 |
| 3. 并发与状态机 | A 的延迟历史/知识库响应进入 B；统计并发丢失 | 【确凿证实】Chromium 合成接口复现切换后泄出 A 检索结果；两个独立 manager 同步读后各加一，最终只有一次 | P2 | 请求代次检查、切换即清空；统计改 SQL 原子累加；已修复 |
| 4. 外部输入与解析 | 知识库内容或伪造附件路径能扩大资源范围 | 【证伪为假警报，限定资源边界】其他身份的文档 ID 无法下载/启停/删除/检索；附件绝对路径在 chat 入参被清除，读取工具再做目录边界检查 | — | 保留已有解析容量、ZIP/PDF 限制；本轮未重新宣称覆盖所有格式解析器漏洞 |
| 5. 渲染与客户端 | 后端正确但浏览器沿用上一 BOT 的提示词、附件、检索词、模态框或缓存 | 【确凿证实】A 延迟知识库结果与搜索词留在 B 面板；修复后 Chromium 延迟检索/历史均不串显示，token 变化触发工作区重载 | P2 | API `no-store` / `Vary: Authorization`；前端身份切换重载与状态清空；已修复。不把 UI 复现升级为后端越权读取 |
| 6. 持久化与 I/O | 技能 SQL 分区但源码目录共享；旧技能被每个用户重新导入 | 【确凿证实】普通用户 GET 技能读到认证前源码；Alice 读回 Bob 同名源码；完整性校验不能阻止源码展示泄露 | P1 | 技能 SQL 与源码目录同范围；不可变内容地址源码；旧根目录隔离；回滚/多进程回归通过 |
| 7. 依赖与环境 | 修复是否要求新中间件或未声明依赖 | 【证伪为假警报，限定本轮】使用已有 SQLite、标准库、前端工具链；新增浏览器验收脚本显式要求 agent-browser；没有新增运行时包或 schema | — | 锁文件、前端构建及全量回归单独验收；OIDC、Linux 和多机按既定计划后置 |
| 8. 假警报证伪 | 共享基础人设就意味着共享所有用户 PROMPT / 知识库 / 历史 | 【部分证实】基础人设/显式共享准则确实共享；但个人历史、记忆、知识库、文件及私有 BOT 名单在双身份 HTTP/提示词测试中不可串读；新的技能/自动学习遗漏见上 | P1（已定位的遗漏） | 不以一个 scope 字段或一轮全量测试概括所有路径；完整回归矩阵见第五章 |

## 二、核心确诊问题深度剖析（仅 P0 / P1）

没有获得可支持 P0 的证据。本章三项均为修复前可重复的 P1，不假定已经发生真实用户数据外泄。
完整后端修复及回归测试差异见 [public-bot-isolation.patch](patches/public-bot-isolation.patch)。
补丁以**本轮开始时的工作区**为基线，包含下面三项及摘要/统计等相关后端修复，不包含前端差异；不能从旧 HEAD 跳过此前文件改造直接套用。

### P1-01：技能记录有用户范围，源码和旧版导入却没有

**文件和行号**：[bot_manager.py](../core/agent/bot_manager.py#L1733)，技能目录、源码解析与读写；
[agent.py](../apis/agent.py#L2571)，技能列表与详情端点。

**Root cause / 调用链**：

1. 旧版 `list_skills` 先按用户范围查询 SQLite。
2. 没有 SQL 行时，读取公共根 `bots/<id>/skills/*.json` 和 `*.py`，并归入当前用户范围。
3. 有 SQL 行时，仍按 `bots/<id>/skills/<name>.py` 读取源码，没有使用该用户的目录。
4. GET 列表、GET 单个技能和 BOT 详情会返回源码。hash mismatch 只禁止执行，原来没有禁止返回实际源码。
5. 不同用户保存同名技能共用一个路径；删除也按同一路径删除。

**Minimal PoC**：以下步骤全部在独立临时 BotManager 目录复现，不需要修改生产资料。

```python
# 使用新增测试文件的 resources fixture / acting_as / identity。
manager, engine, rag, client, state = resources
root = Path(manager._bot_dir('default_assistant')) / 'skills'
(root / 'old_private.json').write_text(json.dumps({
    'name': 'old_private', 'description': 'pre-auth private data'
}))
(root / 'old_private.py').write_text("result = 'PRE_AUTH_PRIVATE_SOURCE'")
response = client.get('/api/agent/bots/default_assistant/skills')
# 修复前：200，response.text 包含 PRE_AUTH_PRIVATE_SOURCE。
# 修复后：200，skills=[]；详情 404，旧源文件仍隔离保留。

with acting_as(identity('alice', admin=True)):
    manager.save_skill('default_assistant', 'diagnose', 'alice', "result = 'alice secret'")
with acting_as(identity('bob', admin=True)):
    manager.save_skill('default_assistant', 'diagnose', 'bob', "result = 'bob secret'")
with acting_as(identity('alice', admin=True)):
    skill = manager.get_skill('default_assistant', 'diagnose')
# 修复前 skill.code 是 Bob 源码，且 integrity_status=mismatch。
# 修复后 Alice 读回自己的 verified 源码，Bob 删除不影响 Alice。
```

**Blast radius**：普通公共 BOT 使用者可通过 GET 触发旧版技能继承；同名覆盖原本需要有技能保存权限的管理员或内部写入路径。
源码中如果有业务标识、用户数据、操作条件会泄露；没有实证证明源码中存在明文凭据，因此没有将其定为 P0。
哈希及 AST 执行门禁仍生效，不能把源码串读直接说成任意远程执行。

**原子修复**：

```diff
 def _skills_dir(self, bot_id):
-    s_dir = os.path.join(self._bot_dir(bot_id), "skills")
+    s_dir = os.path.join(self._runtime_dir(bot_id), "skills")
```

完整补丁还包含：按源码 SHA-256 命名不可变 Blob、临时完整写入后原子发布、同主机锁、SQL 失败回滚新 Blob、未引用源码清理、读取前校验。
旧名称源码仅允许在**当前同范围目录**且哈希匹配时导入；即便历史 SQL 留下了已分区记录，也不从公共根目录补读源码。
对于缺少/错配源码，保留完整性错误用于诊断，返回空 code 并拒绝执行。
公共 BOT 的个人技能 CRUD 已与个人知识库一致，使用 BOT 访问权限；执行仍要求 operator 并经过原有高危门禁。

### P1-02：租户管理员可以修改供其他认证范围使用的全局模板

**文件和行号**：[agent.py](../apis/agent.py#L66)、[bot_manager.py](../core/agent/bot_manager.py#L410)。

**Root cause / 调用链**：`bot_scope_matches` 为方便复用全局公共模板，允许所有认证范围访问空 issuer/tenant 的公共 BOT；
定义修改和管理员旧状态迁移又复用了这条“可以使用”规则，然后只检查 `is_admin`，没有要求管理范围精确匹配。

**Minimal PoC**：

```http
PUT /api/agent/bots/css_live_operator
Authorization: Bearer <测试中模拟的 tenant-a 管理员身份>
Content-Type: application/json

{"base_prompt":"tenant A controls all tenants"}
```

在空范围的全局公共 `css_live_operator` 测试定义下，修复前返回 200。修复后为 403。
同身份向默认助手写共享 guideline、删除全局公共 BOT 或迁移无归属旧状态也被拒绝。
当管理员修改自己 issuer/tenant 范围的公共 BOT 时仍返回 200。

**Blast radius**：依赖该全局模板的其他租户/issuer 会受到人设、共享准则或删除操作影响；旧状态迁移还可能把不明归属资料分给不应管理它的范围。
这是 HTTP 路由级复现，身份由鉴权函数的测试替身注入；没有使用真实签发的 OIDC token。

**原子修复**：

```diff
-if user.is_admin:
-    return user
+if bot_manager.can_edit_bot_definition(
+    bot, user_id=user.subject, is_admin=user.is_admin,
+    issuer=user.issuer, tenant_id=user.tenant_id,
+):
+    return user
```

`can_edit_bot_definition` 要求定义 issuer/tenant 与当前管理范围精确匹配。全局空范围模板可跨范围使用，不能由任意租户管理员维护。
当前没有增加跨租户超级管理员角色；全局模板通过本地管理/受控配置维护。租户管理员可管理本范围定义。
推荐指令“换一换”原本也会写共享定义，现同样要求定义管理权限。

### P1-03：管理员日常对话的个人偏好被自动发布为共享准则

**文件和行号**：[tools.py](../core/agent/tools.py#L407)，`EnhancePromptTool`；
[engine.py](../core/agent/engine.py#L2187)，自动学习提示；[bot_manager.py](../core/agent/bot_manager.py#L361)，共享准则范围。

**Root cause / 调用链**：公共 BOT 的 `enhance_self_prompt` 对普通用户拒绝，对管理员允许；工具直接调用 `add_learned_guideline`。
准则按定义范围保存，并在每个用户的系统提示词中注入。因此角色有管理员权限并不代表其日常对话中提到的内容应自动公开。

**Minimal PoC**：

```python
with acting_as(alice_admin):
    EnhancePromptTool('default_assistant', manager).forward(
        'ALICE_PRIVATE_PREFERENCE', 'private context'
    )
with acting_as(bob_operator):
    assert 'ALICE_PRIVATE_PREFERENCE' in engine.assemble_system_prompt(bot)  # 修复前成立
```

**Blast radius**：同一公共 BOT 的用户会得到管理员个人偏好及 reason 中的上下文，可能带来业务数据披露或持久提示污染。
普通用户原本无法通过这条工具修改共享准则，这部分是被证伪的攻击假设。

**原子修复**：

```diff
 def forward(self, instruction, reason=""):
+    if bot.visibility == "public":
+        # 所有角色的对话学习都属于当前用户。
+        key = "behavior_preference_" + sha256(instruction.encode()).hexdigest()[:16]
+        return manager.save_memory(bot_id, key, instruction)
     return manager.add_learned_guideline(bot_id, instruction, reason)
```

实际实现保留权限、空输入、字符限制；完整代码见补丁。公共 BOT 的个人偏好在“记忆”中查看和删除；
确需影响所有人的规则，管理员必须明确使用共享准则管理入口。
已存在的共享准则不能自动判断哪些是个人内容，因此本轮未批量搬迁/删除。只读检查当前本地数据库时，共享公共准则为 0 条，未发现需人工分配的此类记录。

## 三、P2 / P3 重点弹性与工程边界修复

### 已修复

1. **前端跨 BOT 混显**：[AgentPanel.tsx](../web/src/components/panels/AgentPanel.tsx#L1147)。
   切换时在 layout effect 中清空详情、消息、附件、文件、知识库列表/搜索结果、输入词和编辑模态框。
   历史分页、知识库检索/上传/启停、附件上传、人设保存和资料保存携带请求代次；过期响应不更新当前面板。
   新旧代码分别用相同 Chromium 延迟响应场景验证：旧版 B 面板含 `A_DELAYED_KNOWLEDGE_SECRET`，修复后为 false。

   ```typescript
   const botId = activeBotId
   const requestEpoch = botRequestEpochRef.current
   const data = await response.json()
   if (!isCurrentBotRequest(botId, requestEpoch)) return
   setKnowledgeSearchResults(data.results || [])
   ```

2. **浏览器身份与缓存**：[auth.ts](../web/src/lib/auth.ts#L95)、[core/auth.py](../core/auth.py#L361)。
   API 请求和响应均设置 no-store，响应声明 `Vary: Authorization`。
   当前文档的 token 变化时，在下一次 API 请求或已发请求返回时拒绝继续更新旧工作区并重载；活动任务轮询也会触发检查。
   这不是完整 OIDC 登录/退出功能的替代，也不意味着清除了业务工作台所有 localStorage 参数缓存。

3. **回合摘要写错数据库**：[engine.py](../core/agent/engine.py#L3111)。
   原 `on_turn_completed` 漏传 manager，走离线工具的数据库推断和空范围；实测触发外键失败，当前主库摘要比真实历史少一轮。
   修复为同一个 manager / 同一个用户范围；没有证据显示此故障路径把摘要泄露给其他用户。

   ```python
   memory_manager.on_turn_completed(
       bot_id=bot_id,
       bot_dir=self.manager._runtime_dir(bot_id),
       all_history=self.manager.get_history(bot_id, limit=500),
       saved_state=self._get_sandbox(self._sandbox_key(bot_id)),
       max_context_turns=effective_context_turns,
       manager=self.manager,
   )
   ```

4. **共享运行统计丢失/整定义回写**：[persistence.py](../core/persistence.py#L2136)。
   原先两个 manager 同时读取旧 `BotStats`、各加一、再 `save_bot`，最终只增加一次；这个保存还可能把旧人设覆盖回去。
   现在 SQLite 单语句更新 JSON 中的统计子树，不重写其他定义字段。没有实际升级需要时，默认助手初始化也不再重复保存定义。

   ```python
   def record_run_stats(self, bot_id, metrics):
       self.metadata_store.increment_bot_stats(bot_id, metrics)
   # UPDATE bots SET definition_json = json_set(definition_json, '$.stats',
   #   json_object('total_runs', old_runs + 1, ...)), updated_at = ? WHERE bot_id = ?
   ```

5. **协同事件返回范围和文案**：[agent.py](../apis/agent.py#L2765)。
   日常 BOT 资源页只返回当前用户协同事件；同租户管理员查看全范围事件仍需进入显式管理 topology 入口。
   UI 标出共享准则/个人技能/个人记忆边界，依据服务端 `permissions.can_edit_definition` 禁用无权修改操作；后端仍独立执法。

6. **个人技能资源限制与特殊 subject**：技能每个 BOT/用户最多 100 项，代码最多 64 KiB，描述和入参定义合计最多 32 KiB。
   限额在同范围跨进程锁内检查，超限 API 返回 422，Agent 工具返回明确失败；同名更新允许，另一用户不共用名额。
   `CreateBotTool` 只有真正空 issuer/tenant 的本地 `local` 才允许兼容 ownerless 创建；认证 subject 恰为 `local` 时仍设置其 owner，避免创建后无法访问。

### 仍需明确的工程边界

- **活动 AgentRun / SSE / Python 常驻变量不是共享持久化状态。**
  [run_manager.py](../core/agent/run_manager.py#L314) 和 [engine.py](../core/agent/engine.py#L1946) 的容器仍是进程内字典。
  完整身份键已防止同进程用户串读，但另一个 worker 无法自动重连该任务、承接原审批或恢复变量。
  本轮没有改造跨进程的聊天任务所有权与 SSE 路由，也没有把已有任务引擎的多进程验收当作 Agent 对话验收。
  单机多 worker 稳定使用聊天前，还需明确 worker 亲和/任务拥有者路由，或设计本机 IPC + SQLite 所有权协调。
  **这件事不强制要求 Redis。**只把运行变量塞进 SQLite 也不能替代正在执行的 Python 进程和审批回调。
- 私有 BOT 的管理员访问、管理 topology 和平台云账号是明确管理能力；普通用户无法因此继承管理员权限。
- 旧根目录数据归属不明时保持隔离。当前技能 SQL 中本地空范围记录可继续用于本地开发；不自动分配给后续登录用户。
  错误导入过的历史 scoped 元数据没有人工归属证明时不从根源码补读；可删除后由正确用户重新保存。
  本轮只读检查当前本地库：公共共享准则为 0 条，技能为 4 条且均为空认证范围；此结果不代表其他部署也没有历史误归属记录。
- **两个 Uvicorn worker 的 Agent SSE 恢复属于当前单机多进程目标尚未完成的验收。**应先补齐运行态协调，再验证任务重连、审批、取消和同一用户并发请求；真正 OIDC 签发/登录、Linux/nsjail 主机隔离按既定要求放在最后，多 Pod/多机继续留待后续。
- 本轮的 API/UI 用户隔离审查不等于对所有第三方解析器、模型提供商、宿主机或数据库管理员的防护证明。

## 四、证伪为假警报的分析

| 曾经怀疑的攻击路径 | 为什么在当前链路不成立 | 实测覆盖 |
|---|---|---|
| Bob 直接用公共 BOT ID 读 Alice 的历史/记忆/知识库 | 定义访问权限与资源范围分开；SQL 使用完整身份范围，文件也进入完整身份哈希分区 | 同用户不同 issuer、同用户不同 tenant、不同 subject 及认证 subject=`local` 的 HTTP 反例均返回空/404 |
| 公共 BOT 组装提示词时带出私有 BOT 名单或 Prompt | roster 先做同范围与 owner/public 过滤；显式 ID、模糊名称和 engine 子任务边界再次检查授权 | 普通 Bob 看不到 Alice 私有 BOT 名称，显式协同与直接 engine 委派均被拒绝 |
| 跨轮 sandbox / 实体缓存仅按 bot_id 存放 | 实际使用的 sandbox key 包含 bot/issuer/tenant/subject；`_entity_cache` 目前没有给任务注入实体的读取路径，实体由当前历史提取 | BOT 详情与分层 prompt 不包含其他身份的 sandbox sentinel；后台并发身份测试通过 |
| 所有共享 guideline 都是越权披露 | 基础人设和显式共享准则本来就是公共定义；真正的遗漏是自动把个人会话写入共享准则 | 普通用户共享准则 API/工具旧版已拒绝；管理员自动学习外溢单独复现并修复 |
| 文档 ID 或下载 URL 能跨用户复用 | ID 不是授权；先在当前范围取元数据，路径再限制在当前用户目录 | 列表、检索、原文件下载、启停、删除均检查；Alice 操作不影响 Bob 原文件 |
| `basename` 重写路径等于任意读取 | `basename` 只能在当前受控目录内匹配，随后 `realpath/commonpath` 限制目标；传入另一个用户目录不会获得其目录访问能力 | 完整资源 HTTP 测试和既有 `read_file`/文件边界回归通过；没有报告未经验证的目录遍历 |
| spawn/RPC 子进程默认 local/admin 导致提权 | 工具能力在宿主请求/后台 worker 的已设置 ContextVar 中调度，敏感依赖留在 `_TOOL_DEPS`；后台线程显式设置完整身份与角色 | 真实 RPC 中普通用户保存个人技能/偏好；另一身份不可读取；operator 与审批回归保留 |

## 五、针对性回归测试用例清单（Automated Test Matrix）

新增测试见 [test_public_bot_isolation_audit.py](../tests/test_public_bot_isolation_audit.py)，
与原 [test_public_bot_resources.py](../tests/test_public_bot_resources.py) 及既有鉴权/RAG/文件用例共同验证。

| 用例 | 断言目标 |
|---|---|
| `test_public_skill_source_and_deletion_are_user_partitioned` | 同名技能读回自己的源码，删除不影响另一用户 |
| `test_signed_in_reader_does_not_import_quarantined_public_skills` | 普通已登录 GET 不导入根 JSON/源码，旧文件保留 |
| `test_old_scoped_skill_row_cannot_launder_shared_root_source` | 即便遗留 SQL 有 scoped 记录也不补读无归属根源码 |
| `test_public_skill_crud_is_personal_and_ordinary_users_can_save` | 公共 BOT 普通用户可保存自己技能；跨 subject/issuer/tenant/local 读删隔离 |
| `test_personal_skill_quota_and_metadata_limits_return_422_without_writing` | 个人技能数量/入参超限返回 422，无额外写入，同名更新和不同用户互不影响 |
| `test_authenticated_subject_local_creates_private_bot_owned_by_local` | 认证 subject 为 local 不变成 ownerless，本人可访问新私有 BOT |
| `test_tenant_admin_cannot_edit_global_public_definitions_or_claim_legacy_state` | 阻断全局定义、准则、推荐指令、删除、旧状态迁移；本范围管理成功 |
| `test_public_auto_enhancement_keeps_conversation_preferences_personal` | 普通用户与管理员的自动学习都只进入自己的记忆 |
| `test_prompt_preview_uses_same_user_scope_for_all_memory_layers` | HTTP 详情的历史/摘要/记忆/KB/变量不会注入另一身份，且响应 no-store |
| `test_turn_completion_summary_stays_in_canonical_scoped_database` | 当前主库摘要计数更新至完整历史；无旁路 sqlite3 文件 |
| `test_public_prompt_roster_and_delegation_cannot_reach_another_users_private_bot` | 名单、显式 ID 协同及 engine 直接调用都不能绕过私有 BOT 权限 |
| `test_preference_and_skill_tools_keep_identity_through_real_worker_rpc` | 真实 spawn/RPC 端到端保留普通用户范围，不落为 local/admin |
| `test_public_orchestration_resource_is_personal_even_for_admin` | 日常 BOT 路由管理员不隐式读全用户，显式管理 topology 保持权限 |
| `test_concurrent_background_workers_keep_prompt_history_rag_and_approval_identity` | 两个真实后台线程交错执行仍保留各自 prompt、history、RAG 和角色；其他用户/tenant 审批失败 |
| `test_skill_failed_sql_commit_preserves_previous_code_and_cleans_new_source` | SQL 故障不覆盖旧技能，清理新源码 |
| `test_two_processes_save_same_skill_name_without_crossing_source` | 两个 OS 进程同名技能分开；CRLF 源码仍 verified；20 次统计全部保留 |
| `test_shared_bot_stats_update_does_not_overwrite_other_worker_or_definition` | 同步读旧值的确定性竞态修复，两次运行应累计两次 |
| `scripts/verify_public_bot_ui.py` | Chromium 延迟知识库、延迟历史、切换清空搜索词、token 变化重载 |

浏览器脚本只加载本地前端静态资源，所有 `/api/` 请求在页面启动前替换为合成响应，不修改真实会话/文档/云配置：

```bash
rtk proxy .venv/bin/python scripts/verify_public_bot_ui.py --url http://127.0.0.1:8000
```

后端测试在独立代码/配置/数据副本运行，不直接使用正在运行的 SQLite。HTTP 身份通过测试鉴权函数注入，
模型完整执行使用可控回放；真实 RPC、后台线程、OS 子进程和真实 Chromium 分别单独验证，均不冒充线上 OIDC 或真实模型验收。

最终验收结果：

- 独立副本全量 Python 回归：**406 passed in 69.38s**。本轮新增隔离审查文件覆盖 18 项测试实例；修复前首批 10 项中 8 项失败、2 项通过，用于区分真实遗漏与既有防护。
- Chromium：延迟知识库、延迟历史、搜索词清空和身份 token 改变后重载四项通过，使用合成 API 响应；没有改写真实用户资料。
- 前端 TypeScript 检查及生产构建通过，产物已同步到 `static/`；仍有既存的大 bundle 警告，主 JS 约 3.65 MB，gzip 约 1.13 MB。
- `uv lock --check`、`uv pip check --python .venv/bin/python` 通过，后者检查 63 个包的兼容性；本轮没有新增运行时依赖。
- Python 语法、文档链接、`git diff --check` 及后端补丁反向适用性检查通过。反向检查只做 `git apply --check --reverse`，没有实际回退修复。
- 代码、文档和构建产物均留在工作区供审阅，**未提交、未推送**。这些结果不等于真实 OIDC、Linux/nsjail 或跨 Uvicorn worker 的 Agent 聊天验收。
