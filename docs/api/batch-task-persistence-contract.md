# 批处理持久化 API 契约

本文档描述单机 SQLite 阶段批处理接口的可观察行为。它和
[ADR-0001：持久化分层与任务后端演进](../adr/0001-persistence-and-task-backend.md)
一起维护；实现切换到 PostgreSQL + Redis 后，接口语义应保持不变。

## 任务状态

| 状态 | 含义 | 普通重试行为 |
| --- | --- | --- |
| `RESERVED` | 已写入任务和幂等记录，但 Worker 尚未取得租约 | 允许同一个请求恢复 |
| `RUNNING` | Worker 持有租约并执行中 | 返回原任务，不创建第二个任务 |
| `PAUSED` | 已持久化暂停命令，Worker 在安全检查点等待 | 只能通过 resume 继续 |
| `COMPLETED` | 所有输入行已经处理 | 返回原任务 |
| `FAILED` | Worker 或任务执行失败 | 返回原任务，是否重试由调用方决定 |
| `CANCELLED` | 已收到终止命令 | 返回原任务 |
| `UNKNOWN` | Worker 租约过期，外部请求是否成功无法确认 | 禁止自动重试，必须先人工核对 |

`UNKNOWN` 是防重复副作用的保护状态。它不能被旧 Worker 直接改写为
`COMPLETED`，也不能因为客户端重新发送同一个 `Idempotency-Key` 而自动执行
同一批外部写操作。

目录同步任务使用相同的 `PENDING/RUNNING/COMPLETED/FAILED/CANCELLED/UNKNOWN` 语义。
同步任务的 `RUNNING` 行必须持有 `worker_id + lease_until`；租约过期后会
进入 `UNKNOWN`，旧线程即使晚到也不能写回 `COMPLETED`。仍处于 `PENDING` 且
尚未取得租约的任务，只有在预留已明显超时后才允许通过同一个幂等键恢复，
避免正常启动竞态被第二个请求误判为崩溃。`PENDING` 收到取消请求时会直接
进入 `CANCELLED`，因此迟到的 Worker 不会取得租约或执行外部同步；已经
`RUNNING` 的任务则先设置持久化取消标志，等待当前 HTTP 请求结束后进入
`CANCELLED`。

## 创建任务

```http
POST /api/batch/create
Idempotency-Key: batch-20261106-001
Content-Type: application/json
```

请求体包含 `api_id`、`items`、`delay_sec`、`dry_run` 以及高危接口确认字段。
服务端会把规范化后的请求计算为 fingerprint，并按以下范围保存幂等键：

```text
(issuer, tenant_id, owner_subject, operation_kind, idempotency_key)
```

同一用户使用相同幂等键和相同请求内容时，响应中的 `task_id` 必须保持不变。
相同幂等键对应不同请求内容时返回 `409`，不能静默覆盖原任务。

创建成功响应：

```json
{
  "status": "success",
  "task_id": "2c2f6b8e9a11",
  "total": 1,
  "api_id": "c2c_msg_withdraw",
  "dry_run": false,
  "idempotent_replay": false
}
```

创建过程先写入 `RESERVED` 任务，再尝试取得 Worker 租约。服务进程在这两个
步骤之间退出时，同一个幂等请求可以恢复这个 `RESERVED` 任务；已经是
`RUNNING`、终态或 `UNKNOWN` 的任务不会被第二次执行。

## 查询任务

```http
GET /api/batch/{task_id}
```

响应中的 `data` 至少包含：

```json
{
  "task_id": "2c2f6b8e9a11",
  "api_id": "c2c_msg_withdraw",
  "owner_id": "operator-1",
  "status": "RUNNING",
  "current": 4,
  "total": 10,
  "success": 4,
  "failed": 0,
  "error": null,
  "created_at": 1730870400.0,
  "started_at": 1730870400.1,
  "finished_at": null,
  "updated_at": 1730870402.2
}
```

查询、事件流、控制和历史详情都必须经过同一条 owner/admin 权限链路。
跨进程查询以 SQLite 中的 `batch_tasks` 为准，不能只查当前进程的
`TaskRunner.tasks` 缓存。

任务的持久化范围为：

```text
(issuer, tenant_id, owner_subject)
```

同一个 `sub` 在不同 issuer 或 tenant 下不能读取、控制或复用同一个任务。
本地开发模式没有 OIDC claims 时三者为空，保持单租户兼容。

## 事件流

```http
GET /api/batch/{task_id}/events
Accept: text/event-stream
```

Worker 发出的 `start`、`progress`、`complete`、`error` 事件会写入
`task_events`，SSE 重连时先从数据库按序列回放，再等待新事件。

如果任务已经进入终态但因进程在“写状态”和“写终态事件”之间退出而没有
终端事件，SSE 必须结束连接，客户端随后通过任务查询接口读取最终状态，不能
无限保持连接。

批处理结果正文保留在 JSONL/Blob 文件中，SQLite 保存逐条结果的 sequence、
状态、错误码、哈希、字节偏移、长度和 URI。读取或导出前如果发现结果文件
丢失，或者文件中的有效行数与索引数量不一致，接口返回明确错误，不把它当
成空结果。

## 控制任务

```http
POST /api/batch/{task_id}/pause
POST /api/batch/{task_id}/resume
POST /api/batch/{task_id}/stop
```

控制命令先写数据库，再唤醒当前进程中缓存的 Worker。这样第二个进程也能
控制第一个进程创建的任务。

`stop` 具有以下持久化保证：

- `batch_tasks.status = 'CANCELLED'`；
- `finished_at` 只在首次终止时写入；
- `worker_id` 和 `lease_until` 清空；
- 对应 `idempotency_keys` 同步为 `CANCELLED` 并清除租约；
- 已经是 `COMPLETED`、`FAILED` 或 `CANCELLED` 的任务不会被覆盖。

对活动任务执行历史删除返回 `409`。管理员清空历史时只删除
`COMPLETED`、`FAILED` 和 `CANCELLED` 记录，并保留 `RUNNING`、`PAUSED`、
`UNKNOWN` 任务及其结果文件。

## 单机部署边界

当前 SQLite 契约只支持单机共享本地磁盘：

- 单进程：SQLite 和本地 Blob 目录即可运行，不需要 Redis。
- 多进程：所有进程必须打开同一个 SQLite 文件和同一个 Blob 根目录。
- SQLite 禁止放在 NFS、对象存储挂载或无法保证文件锁语义的网络盘上。
- 多主机、多 Pod 时，数据库事实迁移到 PostgreSQL；Redis 只承载任务分发、
  短期租约、实时事件和限流，不能成为唯一事实来源。
