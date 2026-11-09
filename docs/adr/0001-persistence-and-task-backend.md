# ADR-0001：持久化分层与任务后端演进

## 状态

**Accepted（分阶段实施）**

日期：2026-11-07

这份 ADR 确定当前单机部署和未来 PostgreSQL + Redis 部署的共同数据边界。它不要求当前版本立即引入 Redis，也不把“所有文件都迁入 SQLite”作为目标。

应用自身的运行时命名已经在 2026-11-08 由
[ADR-0002：TC Studio 命名空间迁移与 TIM 兼容窗口](0002-tc-studio-namespace-compatibility.md)
统一为 `TC_STUDIO_*`、`tc_studio.sqlite3` 和 `/var/lib/tc-studio`。本文中
`TIM_*`、`tim.sqlite3` 和 `/var/lib/tim` 仅在描述旧部署、迁移源或历史方案时保留。

## 背景

历史版本和当前运行期同时存在几种持久化方式。下面列出的 BOT/RAG JSON 路径是旧部署的迁移源或兼容回退源，不再是新的运行期结构化事实源：

- `storage/batch_idempotency.sqlite3`：批处理幂等键。
- `storage/history.json`：批处理历史摘要。
- `storage/batch_results/task_*.jsonl`：批处理明细。
- `data/bots/*/bot.json`：旧 BOT 定义、owner 和可见性迁移源；当前事实在 `bots`。
- `data/bots/*/history.json`、`history.jsonl`：旧 BOT 会话历史迁移源；当前事实在 `conversation_sessions` / `conversation_messages`。
- `data/bots/*/memory.json`、`session_summary.json`：旧长期记忆和会话摘要迁移源；当前事实在 `bot_memories` / `bot_session_summaries`。
- `data/bots/*/skills/*.json`：旧技能元数据迁移源；当前事实在 `bot_skills`，源码仍在 `.py` 文件。
- `data/bots/*/knowledge/docs.json`、`chunks.json`：旧 RAG 文档和分块索引迁移源；当前事实在 `rag_documents` / `rag_chunks`。
- `data/orchestrations.json`：旧多智能体协同流水迁移源；当前事实在 `orchestration_events`。
- `data/bots/*/uploads/*`、`knowledge/files/*`：上传文件和生成文件。
- `config.json`、`data/llm_config.json`：环境配置和大模型配置，其中可能包含密钥。
- `withdraw_tasks.json/csv`、`withdraw_results.json/csv`：CLI 人工输入与导出文件；Web 批处理事实不依赖它们。
- `.history.lock`、`.rag.lock`、JSONL `.idx`：跨进程锁和可重建索引，不是业务事实。
- `${TC_STUDIO_STORAGE_DIR}/docs/*.json`、`${TC_STUDIO_STORAGE_DIR}/official_live_tree.json`、
  `${TC_STUDIO_STORAGE_DIR}/docs_cache/*`：官方目录、文档和同步缓存；默认开发目录仍是
  仓库下的 `storage/`。
- `core/*_catalog.json`、`core/live_common_specs.json`、`core/live_data_types.json`：
  随代码发布的只读 fallback。运行期同步覆盖写入
  `${TC_STUDIO_STORAGE_DIR}/catalogs/*_full_catalog.json` 和
  `${TC_STUDIO_STORAGE_DIR}/*_common_specs.json` / `*_data_types.json`。
- `core/task_runner.py` 的 `TaskRunner.tasks` 和 `core/sync_task_manager.py` 的任务字典：运行期对象缓存，不能作为跨进程事实来源。

SQLite 过去只负责批处理幂等预留；当前阶段已经把任务事实、事件、同步任务状态和 BOT/RAG 结构化元数据纳入统一主库。它仍不能单独解决多主机、多 Pod 的队列调度和自动接管。

## 决策驱动因素

需要同时满足以下要求：

1. 单机单进程可以低成本运行，不强制依赖 Redis。
2. 单机多进程可以共享关键状态，避免依赖某个进程的内存。
3. 所有用户和租户相关数据必须有明确的归属和审计边界。
4. 大文件不能直接塞进关系数据库。
5. 密钥不能因为“统一持久化”而被复制到普通业务表。
6. 未来切换 PostgreSQL + Redis 时，业务代码不应重新改写所有存储调用。
7. 任务执行失败、进程崩溃、网络超时和 Worker 重启后，系统必须能区分“未执行”“执行中”“已完成”和“结果未知”。

## 决策

采用四层持久化模型：

```text
业务事实与任务状态       SQLite（当前） → PostgreSQL（多主机生产）
实时队列与事件分发       进程内队列（当前） → Redis Streams/PubSub（高并发阶段）
大文件和生成物           本地 Blob 目录（当前） → 对象存储
密钥和外部凭据           环境变量/密钥管理服务，不进入普通 SQLite 业务表
```

SQLite 是当前单机阶段的事务数据库，不是未来架构的永久边界。代码通过存储接口访问数据，底层实现可以从 SQLite 替换为 PostgreSQL，业务层不直接依赖具体数据库驱动。

### 依赖锁定与可重复构建

Python 版本由 `pyproject.toml` 固定为 `>=3.11,<3.14`，`.python-version` 默认选择 3.13。`uv.lock` 是解析结果的主锁文件，`requirements.lock` 是带完整 hashes 的生产 pip 锁文件，`requirements-dev.lock` 是带开发依赖的锁文件。

生产安装必须使用：

```bash
./scripts/install_locked.sh
```

该入口执行 `uv sync --locked --no-dev`、依赖一致性检查和源码编译检查，不允许安装过程中重新解析或改写锁文件。`requirements.txt` 只保留范围版本兼容入口，不能作为生产可重复构建依据。

### 当前阶段的推荐边界

单机单进程和单机多进程阶段统一使用一个主数据库文件：

```text
storage/tc_studio.sqlite3
```

现有单独的 `batch_idempotency.sqlite3` 应迁移为主数据库中的 `idempotency_keys` 表，避免一个系统同时维护多份 SQLite 事实库。

数据库路径必须可配置，例如：

```text
TC_STUDIO_DATABASE_URL=sqlite:////var/lib/tc-studio/tc_studio.sqlite3
```

旧的 `TIM_DATABASE_URL`、`TIM_SQLITE_PATH` 和其他 `TIM_*` 变量仍可读取，但
`TC_STUDIO_*` 始终优先；默认位置只有 `storage/tim.sqlite3` 时继续使用旧库，
新旧库同时存在时只使用 `storage/tc_studio.sqlite3`，不会自动合并。

单机多进程允许使用 SQLite，但必须满足：

- 所有进程访问同一个本地持久化文件。
- 不把 SQLite 放在 NFS、对象存储挂载或不保证文件锁语义的网络文件系统上。
- 启用 WAL、忙等待、外键检查和明确的同步级别。
- 数据库文件、父目录和备份文件使用最小权限。
- 任务状态、历史和幂等记录都进入同一个数据库。
- 对外声明当前是单机共享存储模型，不支持多主机写入。

### 未来多主机阶段的推荐边界

当部署变成多 Pod、多主机或需要自动扩缩容时：

- PostgreSQL 保存业务事实、任务状态、幂等键、审计记录和任务明细元数据。
- Redis 保存任务队列、短期租约、实时事件和 SSE 发布订阅。
- 对象存储保存上传原件、生成文件、RAG 原文件和大型导出文件。
- PostgreSQL 仍然是任务状态和审计事实的最终来源。
- Redis 丢失时可以重建实时事件；PostgreSQL 丢失时不能靠 Redis 恢复完整业务事实。

Redis 不是当前单机阶段的强制依赖。必须引入的是“共享、持久、可恢复的任务状态”，而不是某个特定产品。

## 当前实施状态

截至 2026-11-07，已经落地第一阶段的存储骨架：

- 新增 `core/persistence.py`，统一解析 `TC_STUDIO_DATABASE_URL` /
  `TC_STUDIO_SQLITE_PATH`，并兼容旧的 `TIM_DATABASE_URL` / `TIM_SQLITE_PATH`。
- 默认主库为 `storage/tc_studio.sqlite3`；只发现旧的 `storage/tim.sqlite3` 时会继续使用旧库。
- 配置文件路径支持 `TC_STUDIO_CONFIG_PATH`；生产单机可把带凭据的配置放在
  `/var/lib/tc-studio/config.json`，不必让运行期进程写代码发布目录。
- SQLite 启用 WAL、外键检查、忙等待和 `0700/0600` 权限尝试。
- 创建了 `schema_migrations`、`idempotency_keys`、`batch_tasks`、`batch_task_tombstones`、
  `task_events`、`sync_jobs`、`app_settings`、`audit_events`、`orchestration_events`、
  BOT、文件和 RAG 元数据表。
- 批处理幂等写入已经改用主库中的 `idempotency_keys`。
- 旧的 `storage/batch_idempotency.sqlite3` 会在首次打开主库时导入，源文件保留，便于回滚和人工核对。
- `IDEMPOTENCY_DB_PATH` 仍保留为兼容测试入口，但默认值已经指向统一主库。
- `TaskRunner` 已将任务预留、输入快照、进度、租约、心跳、终态和 `task_events` 写入 SQLite；跨进程查询、SSE 事件回放和 pause/resume/stop 控制已走数据库。
- 远程 stop 会写入终态时间并清理幂等租约；租约过期的任务进入 `UNKNOWN`，旧 Worker 不能把它覆盖成 `COMPLETED`。
- `SyncTaskManager` 已将同步任务的预留、状态、结果和幂等重放写入 SQLite，另一个进程可以读取同一任务状态。
- `sync_jobs` 已补充 `worker_id`、`lease_until`、`last_heartbeat`、`attempt`、进度和取消标志；
  `/api/docs/sync/batch` 也使用同一任务表，租约过期进入 `UNKNOWN`，旧 Worker 不能覆盖终态。
- 批处理结果正文继续写 JSONL，但 SQLite 已保存逐条 sequence、状态、错误码、哈希、偏移、长度和结果 URI；结果文件缺失或索引不一致时导出接口拒绝伪造空结果。
- `CurrentUser` 已从已验证 claims 提取 `issuer`、`tenant_id`；批处理和同步任务的幂等、查询和控制范围均包含 issuer、tenant、subject。
- 已增加统一的审计写入适配器，批处理创建、幂等冲突、高危确认拒绝、任务控制、历史清理、同步启动和同步终态会写入脱敏事件。
- 高危规则已经从 `security_config.json` 切换到 `app_settings`；旧 JSON 只做兼容导入，
  管理员更新会写入 `audit_events`。`data/llm_config.json` 在创建、读取和保存时固定为
  `0600`，其中的 API Key 不进入普通 SQLite 设置表。
- `core/file_storage.py` 为官方目录、文档缓存和公共规范提供跨进程锁、临时文件、
  `fsync` 和 `os.replace`；这些文件仍是可重建缓存，不是业务事实源。
- 多智能体协同流水已经写入 `orchestration_events`；旧 `data/orchestrations.json`
  只允许在 SQLite 表为空时做一次兼容导入，后续写入不会再整文件读改写。
- 已提供 JSON 元数据只读扫描/追加迁移命令，以及 SQLite 备份、恢复和单机部署检查脚本。
- BOT 定义、长期记忆、会话摘要、会话消息、技能元数据、文件引用、RAG 文档和 chunks 的运行期读写已经切换到 SQLite；旧 JSON/JSONL 只在迁移或缺少对应 SQLite 行时兼容读取，新写入不会创建这些旧结构化文件。
- 上传原件、RAG 原文档、技能 Python 源码和大型批处理结果继续放在本地 Blob/文件层，SQLite 保存其归属、状态、哈希、大小、URI 和索引。
- 已提供独立进程 SQLite 验收脚本，覆盖并发幂等、跨进程控制、事件回放、过期租约
  和旧 Worker 防覆盖；同时修复了首次并发打开数据库时的 WAL/schema 初始化竞态。
- 已在 Linux 容器中用降权用户完成单机多进程验收：真实 Worker `SIGKILL`、租约过期、
  `UNKNOWN` 终态防旧 Worker 覆盖、结果文件截断/篡改/删除/不可读、只读数据库拒绝写入，
  以及两个独立 Uvicorn 进程共享同一 SQLite 的 HTTP 幂等和状态可见性。
- 幂等记录清理只针对 `COMPLETED`、`FAILED`、`CANCELLED`；`UNKNOWN` 永久保留，避免
  外部请求结果未知时因保留期到期再次执行同一副作用。
- 部署检查已兼容 Linux `mount`/`findmnt` 输出；无法识别数据库或运行期目录的文件系统类型
  时直接失败，避免把“无法判断是否网络盘”误当成安全。

当前单机多进程 Linux 生产边界已经通过验收，但以下内容仍未完成，不能把本阶段扩大解释为
完整的跨主机故障恢复或所有业务数据迁移：

- `TaskRunner.tasks` 仍是运行期缓存；跨进程事实以 SQLite 为准。
- `data/orchestrations.json` 仍可能作为旧版本兼容源存在，但不再接收新写入；
  删除或归档旧快照不会影响 SQLite 中已经导入的协同事件。
- 批处理历史 JSON 仍保留为兼容快照，但只允许向 SQLite 做一次性补缺导入；
  新写入不再回写 JSON，删除会写 `batch_task_tombstones`，防止旧快照复活已删除任务。
  批处理结果正文仍使用 JSONL/Blob；SQLite 保存任务状态、逐条索引、哈希、偏移、长度和结果 URI。
- Worker 被杀死后的批处理目前进入 `UNKNOWN`，不会自动接管或重试；这条边界是为了避免外部请求已成功但响应丢失时产生重复副作用。
- `SyncTaskManager` 的自动恢复只接管尚未开始执行且仍为 `PENDING` 的安全窗口；已经进入 `RUNNING` 后租约失效仍进入 `UNKNOWN`，不会自动重放外部写操作。
- BOT、会话、记忆、技能和 RAG 的 SQLite 目标表、只读扫描和追加迁移已经接入运行期读写；旧 JSON/JSONL 只作为迁移/兼容源，新写路径不会回写旧结构化文件。
- 历史 JSON 中重复的 RAG 文档 ID 会使用稳定的 `~legacy-<hash>` 后缀消歧，
  原始 ID 会保存在文档元数据中，避免同一个 SQLite 主键覆盖另一份源文档。
- 重复或非法的 RAG 切片序号会分配未使用的序号，并在切片元数据中保留原始序号；
  无法通过 `doc_name/title` 唯一归属到重复文档的切片会跳过并进入迁移报告。
- 具名公共 BOT 用户分区若没有可信 subject 映射会被迁移工具跳过，防止把无法归属的数据错误授予所有用户。

因此当前版本的准确定位是：**统一 SQLite 主库已经接入批处理事实、事件、控制、同步任务状态和
BOT/RAG 结构化元数据；单机多进程的共享状态、幂等、控制、租约失效保护和双 HTTP 实例可见性
已经通过 Linux 验收；自动接管和多主机调度仍属于后续阶段。** `UNKNOWN` 仍需要人工核对后
决定是否重新发起外部操作。

## 数据分类与迁移结论

### 必须迁入 SQLite 的数据

这些数据会被频繁修改、需要事务、需要按用户或租户查询，或者会影响权限和任务正确性。

| 遗留来源或当前事实 | SQLite 目标 | 优先级 | 原因 |
| --- | --- | --- | --- |
| `storage/batch_idempotency.sqlite3` | `idempotency_keys` | P0 | 已经是 SQLite，迁入统一主库，补充状态、租约和过期清理。 |
| `storage/history.json` | `batch_tasks` + `batch_task_tombstones` | P0 | 只做兼容导入和观察期快照，运行期事实不能依赖跨进程 JSON 覆写；墓碑阻断旧任务复活。 |
| `storage/batch_results/task_*.jsonl` 的索引和状态 | `batch_task_items`、`task_results` 元数据 | P0 | 需要分页、失败重试、断点恢复和结果状态。大结果正文可以留对象存储。 |
| `TaskRunner.tasks` | `batch_tasks`、`task_leases` | P0 | 进程重启后必须能恢复任务状态。 |
| `SyncTaskManager` 内存字典 | `sync_jobs`、`sync_idempotency_keys` | P1 | 目录同步不能只在当前进程内可见。 |
| `data/bots/*/bot.json`（遗留迁移源） | `bots` | P1 | owner、visibility、租户归属和更新时间属于权限事实；当前运行期已由 SQLite 负责。 |
| `memory.json`、`session_summary.json`（遗留迁移源） | `bot_memories`、`bot_session_summaries` | P1 | 需要按用户分区、并发更新和清理；当前运行期已由 SQLite 负责。 |
| `skills/*.json`（遗留迁移源） | `bot_skills` | P1 | 技能名称、权限、版本和审计信息需要结构化查询；技能代码正文继续在文件层。 |
| `data/bots/*/history.json`、`history.jsonl`（遗留迁移源） | `conversation_messages`、`conversation_sessions` | P1 | 会话分页、用户隔离、删除和审计由数据库事务保证。 |
| `knowledge/docs.json`（遗留迁移源） | `rag_documents` | P1 | 文档 owner、BOT、状态、版本和删除状态需要事务保护。 |
| `knowledge/chunks.json` 的索引和文本（遗留迁移源） | `rag_chunks` 或后续向量数据库 | P1 | 当前先放 SQLite；数据量和检索规模上升后再拆向量索引。 |
| `data/orchestrations.json`（遗留迁移源） | `orchestration_events` | P1 | 多进程追加不能依赖读全量 JSON 后整文件覆写；拓扑和用户流水需要共享、限量、可按 owner 查询。 |
| 文件引用关系 | `bot_files`、`rag_files` | P1 | 文件归属、哈希、大小、内容类型、删除状态和引用关系必须可验证。 |
| 高危规则和非敏感应用设置 | `app_settings` | P2 | 避免多个进程读取不同版本的配置。 |
| 审计事件 | `audit_events` | P0 | 高危调用、批处理、权限变更和数据迁移需要不可混淆的审计记录。 |

这里的“迁入 SQLite”指迁移结构化元数据和小型文本事实，不是把所有内容都转换成一个超大的 JSON 字段。

### 应继续放文件或对象存储的数据

| 当前来源 | 当前阶段 | 未来阶段 | 原因 |
| --- | --- | --- | --- |
| `uploads/*` | 本地 Blob 目录 | 对象存储 | 可能是图片、PDF、Office、导出文件，不适合塞进数据库。 |
| `knowledge/files/*` | 本地 Blob 目录 | 对象存储 | RAG 原文件需要保留、下载和重新解析。 |
| 大型批处理 CSV/ZIP 导出 | 本地 Blob 目录 | 对象存储 | 结果正文大，数据库只保存 URI、哈希、大小和状态。 |
| `${TC_STUDIO_STORAGE_DIR}/docs_cache/*` | 文件缓存 | 文件缓存或对象存储 | 可从官方来源重建，不是业务事实。 |
| `${TC_STUDIO_STORAGE_DIR}/docs/*.json` | 文件缓存 | PostgreSQL/对象存储均可 | 若只读和整包刷新，继续文件；若需要按记录查询、增量更新和多实例同步，再迁入 `catalog_documents`。发布包 `storage/docs` 只作 fallback。 |
| `official_live_tree.json` | 文件快照 | `catalog_snapshots` + Blob | 作为可回滚快照保存，不能成为唯一运行状态。 |
| `core/*_catalog.json`、公共规范 JSON | 只读发布 fallback；运行期覆盖在 storage | PostgreSQL 缓存表或版本化 Blob | 这是公共目录缓存，不是用户私有数据。多进程读取运行期覆盖时按文件版本自动 reload。 |
| `withdraw_tasks.json/csv`、`withdraw_results.json/csv` | CLI 输入/导出文件 | 对象存储或批处理 Blob | 这是人工操作的交换文件；Web 批处理已经以 SQLite 任务和结果索引为事实源。 |
| `.history.lock`、`.rag.lock`、JSONL `.idx` | 锁/可重建索引 | 本地临时文件或进程协调组件 | 这些文件不承载业务事实，迁入 SQLite 反而会增加无意义的写放大。 |
| 前端 `static/` | Git/发布包 | Git/发布包或 CDN | 不属于运行期业务数据。 |

### 不应因为引入 SQLite 而迁移的内容

#### 密钥和凭据

`config.json`、`llm_config.json` 可能包含云密钥、模型 API Key 或签名材料。它们不应简单迁入普通 SQLite 表。

单机阶段应使用：

- 环境变量或受限权限的本地密钥文件。
- 文件权限 `0600`，父目录 `0700`。
- 不进入备份、日志和普通导出。

多主机阶段应使用企业密钥管理服务或云 KMS/Secret Manager。SQLite/PostgreSQL 只保存凭据引用、版本和轮换状态，不保存明文密钥。

#### 可重建的官方缓存

官方 API 目录、文档详情和公共规范可以继续文件化缓存。只有当它们需要跨实例增量更新、复杂查询、版本回滚或强一致读取时，才需要进入数据库。

## 统一存储接口

业务代码不应继续直接拼接 `history.json`、`memory.json`、`docs.json` 或 SQLite 文件路径。应逐步抽象以下接口：

```python
class TaskStore(Protocol):
    def reserve_idempotency(...): ...
    def create_task(...): ...
    def get_task(...): ...
    def acquire_lease(...): ...
    def append_result(...): ...
    def append_event(...): ...

class BotStore(Protocol):
    def get_bot(...): ...
    def save_bot(...): ...
    def list_history(...): ...
    def save_memory(...): ...

class BlobStore(Protocol):
    def put(...): ...
    def open(...): ...
    def delete(...): ...
    def stat(...): ...

class SecretStore(Protocol):
    def get_reference(...): ...
    def rotate(...): ...
```

建议的实现名称：

```text
SqliteTaskStore
SqliteBotStore
LocalBlobStore
EnvSecretStore

PostgresTaskStore
PostgresBotStore
ObjectStorageBlobStore
RedisTaskQueue
RedisEventBus
SecretManagerStore
```

这样从 SQLite 切换到 PostgreSQL 时，主要替换适配器和迁移脚本，不需要把所有 API 和 Agent 逻辑重新改一遍。

## SQLite 阶段的目标表

第一版不追求一次性覆盖所有业务，先完成会影响正确性和安全性的表：

```text
schema_migrations
tenants
users
bots
bot_memories
bot_session_summaries
conversation_sessions
conversation_messages
bot_skills
bot_files
rag_documents
rag_chunks
batch_tasks
batch_task_items
task_events
task_leases
idempotency_keys
sync_jobs
audit_events
app_settings
orchestration_events
catalog_snapshots
```

文件和对象只在数据库中保存引用：

```text
blob_uri
sha256
size_bytes
content_type
original_name
created_at
deleted_at
```

所有业务表必须带上可用的归属字段：

```text
tenant_id
owner_subject
created_by
created_at
updated_at
```

幂等键建议使用以下唯一约束：

```text
(issuer, tenant_id, owner_subject, operation_kind, idempotency_key)
```

如果当前部署只有单一 OIDC issuer，也保留 `issuer` 字段，为未来多租户迁移提供稳定边界。

## 当前代码必须补的修复

在继续扩大 SQLite 使用前，先处理以下问题：

1. **统一数据库路径**：把 `IDEMPOTENCY_DB_PATH`、任务事实和未来新表统一到 `TC_STUDIO_DATABASE_URL`。**已完成第一阶段，旧 `TIM_DATABASE_URL` 仍兼容读取。**
2. **补幂等记录状态**：增加 `RESERVED`、`RUNNING`、`COMPLETED`、`FAILED`、`UNKNOWN` 和 `EXPIRED`。**已完成批处理主链路。**
3. **补任务租约和心跳**：进程崩溃后可以判断是否接管。**批处理和同步任务均已完成租约、心跳和 `UNKNOWN` 判定；只自动恢复尚未开始执行的安全预留，不自动重放已进入执行阶段的外部副作用。**
4. **修复预留后崩溃窗口**：不能只看到幂等键存在就返回“成功重放”。**已完成 `RESERVED` 任务恢复边界。**
5. **补跨进程任务查询和控制**：SSE、暂停、继续、终止不能只依赖 `TaskRunner.tasks`。**已完成批处理查询、事件回放和控制写入。**
6. **补跨进程历史写入保护**：BOT 会话历史和批处理历史事实都已经使用 SQLite 事务；
   批处理旧 JSON 只做兼容导入，删除墓碑阻断复活。
7. **补幂等记录全量清理**：对 `created_at` 建索引，定期删除过期记录。**已完成安全清理；
   只删除终态可复用记录，`UNKNOWN` 不过期。**
8. **修正错误码**：非法幂等键和任务参数返回 `422`，资源饱和返回 `503`，只有供应商限流返回 `429`。**批处理参数链路已覆盖，仍需继续核对全部接口。**
9. **补逐条业务幂等**：任务幂等不能替代外部腾讯云操作的结果确认和重复执行保护。**仍未完成，`UNKNOWN` 只阻止自动重复副作用。**
10. **同步迁移 `SyncTaskManager`**：不能只修批处理，目录同步和直播目录同步也需要持久化状态。**状态、幂等、租约、心跳、进度、取消和旧 Worker 防覆盖已完成。**
11. **统一租户边界**：所有任务、BOT、RAG、文件和审计查询必须带 `tenant_id` 与 `owner_subject`。**任务、同步、BOT/RAG 主链路已使用 issuer/tenant/subject；OIDC 的真实 issuer/JWKS 绑定仍后置验收。**
12. **补审计链路**：管理员迁移旧数据、修改凭据、审批高危操作、删除文件和批处理执行都要写入审计事件。**批处理、同步、高危规则配置、历史清理、元数据迁移和高危确认拒绝已覆盖；文件级 BOT 操作仍按现有文件元数据审计边界继续补齐。**

## 分阶段实施计划

### Phase 0：定义边界与备份

- 增加 `schema_migrations`。
- 定义 `TC_STUDIO_DATABASE_URL`、`TC_STUDIO_TENANT_MODE`，Blob 根目录由部署检查和文件层配置管理。
- 导出当前 JSON、JSONL、RAG 元数据和文件清单。
- 计算文件 SHA-256，记录迁移批次和源文件路径。
- 迁移脚本必须可重复执行，不能覆盖已有用户数据。

### Phase 1：单机 SQLite 主库

- 创建 `storage/tc_studio.sqlite3`。
- 先迁移 `batch_idempotency`、批处理历史、任务状态和同步任务。
- 再迁移 BOT 元数据、会话、记忆、技能和 RAG 元数据。
- 文件继续留在本地 Blob 目录，数据库只保存引用。
- 新代码从 SQLite 读取和写入，旧文件只作为迁移回退或兼容源。
- 已停止写 BOT/RAG 旧结构化 JSON，保留只读兼容窗口；上传原件、RAG 原文档和技能源码继续在文件层。

### Phase 2：单机多进程

- 所有 Worker 共享同一个本地持久化 SQLite 文件。
- 使用 WAL、跨进程锁和任务租约。
- SSE 改为读取数据库事件或短轮询数据库，不能直接依赖某个进程的内存队列。
- 任务控制通过数据库状态写入，执行 Worker 通过心跳读取。
- 明确禁止把 SQLite 放在 NFS 或临时容器卷。
- **当前阶段已完成并通过 Linux 验收**：两个独立 HTTP 进程共享幂等和任务状态；
  真实 Worker 被 `SIGKILL` 后进入 `UNKNOWN`，旧 Worker 不能终态覆盖，结果文件损坏会被拒绝。

### Phase 3：PostgreSQL

- 保持 `TaskStore`、`BotStore` 和 `BlobStore` 接口不变。
- 使用 PostgreSQL migration 把 SQLite 表迁移过去。
- 优先迁移任务、幂等、审计、用户、BOT 和 RAG 元数据。
- 使用 PostgreSQL 事务、行锁和租约替代 SQLite 文件锁。
- 使用连接池和迁移版本控制。

### Phase 4：Redis

Redis 只在实时和调度需求出现时加入：

- 任务队列。
- Worker 领取任务。
- SSE 事件发布订阅。
- 短期锁和速率限制。
- 临时进度缓存。

Redis 中的数据必须可以从 PostgreSQL 重建，不能把 Redis 当作唯一审计事实来源。

## 迁移策略

迁移必须遵守以下顺序：

```text
备份
  ↓
扫描并校验源文件
  ↓
导入 SQLite 临时表
  ↓
校验数量、归属、哈希和引用
  ↓
切换读路径
  ↓
观察期内双读比对
  ↓
停止旧文件写入
  ↓
保留只读回滚窗口
  ↓
删除旧格式前再次备份
```

迁移失败时，不能出现“数据库已经写了一部分、旧文件也被删除”的状态。所有删除操作必须延后到观察期结束之后。

公共 BOT 认证前旧数据仍然遵守现有规则：不能自动分发，只有管理员明确指定 `target_user_id` 后才迁移。

## 验收标准

### 单机 SQLite

- 两个独立进程同时使用同一个幂等键时，只产生一个任务。
- 任务进程在创建任务后、启动前崩溃，重试不会伪装成成功重放。
- 服务重启后可以查询任务状态和结果。
- 两个进程同时写历史，记录不互相覆盖。
- 过期幂等记录可以被清理。
- 文件和数据库中的 owner、tenant、哈希、数量一致。

### 单机多进程

- **已通过**：Worker A 创建任务，Worker B 能查询状态。
- **已通过**：Worker B 可以通过共享状态暂停、继续和终止 Worker A 的任务。
- **已通过**：事件可以从数据库按序回放；终态以任务查询和结果一致性为准。
- **已通过**：Worker A 被真实 `SIGKILL` 后，任务被标记为 `UNKNOWN`，旧 Worker 不能覆盖；
  自动接管已开始的外部副作用仍明确禁止。

### PostgreSQL + Redis

- 多 Pod 使用同一个幂等键只执行一次。
- Redis 重启后，PostgreSQL 中的任务事实不丢失。
- Redis 事件丢失后，客户端可以通过 PostgreSQL 状态和结果恢复。
- 租户之间不能读取或控制对方任务、BOT、RAG 和文件。
- 数据库备份可以恢复任务、审计和归属关系。

## 后果

### 正面影响

- 单机阶段不需要立刻引入 Redis。
- 关键数据从进程内存和分散 JSON 文件中抽离出来。
- 文件和结构化数据职责分开，避免把大文件塞进数据库。
- 未来迁移 PostgreSQL 时，业务层接口可以保持稳定。
- Redis 只承担实时和调度职责，故障时不会丢失最终业务事实。

### 代价

- 当前需要增加 SQLite schema、迁移脚本和存储适配层。
- 单机多进程阶段仍受到 SQLite 单写者和本地磁盘的限制。
- 需要维护文件 Blob 与数据库引用的一致性。
- PostgreSQL + Redis 阶段需要连接池、队列、租约、监控和备份体系。

### 明确不接受的状态

- 用 SQLite 幂等表掩盖任务仍然只存在内存的事实。
- 多 Pod 各自使用自己的本地 SQLite，却宣称全局幂等。
- 把密钥和大文件直接塞入普通 SQLite 表。
- 只迁移批处理，不迁移同步任务、审计记录和历史写入。
- Redis 中有任务但 PostgreSQL 没有最终事实。

## 相关代码

- [core/task_runner.py](../../core/task_runner.py)：批处理任务、幂等表、历史和结果文件。
- [core/sync_task_manager.py](../../core/sync_task_manager.py)：同步任务状态、幂等、租约和终态。
- [core/agent/bot_manager.py](../../core/agent/bot_manager.py)：BOT、历史、记忆、技能和文件。
- [core/agent/rag_manager.py](../../core/agent/rag_manager.py)：RAG 文档、分块和原文件。
- [core/config.py](../../core/config.py)：环境配置与敏感字段处理。
- [app.py](../../app.py)：任务 API、SSE、暂停、继续、终止和导出接口。
- [deploy/systemd/tc-studio-api@.service](../../deploy/systemd/tc-studio-api@.service)：单机多实例的
  Linux 进程生命周期模板；旧的 `tim-api@.service` 仅作为兼容入口保留。
- [core/namespace.py](../../core/namespace.py)：TC Studio 新命名空间与旧 TIM 配置兼容读取。
