# SQLite 单机运行与验收手册

本文档定义 TC Studio 当前支持的 SQLite 部署边界。它解决的是“单机可以稳定运行、进程重启后状态可恢复、备份可以验真”的问题，不把 SQLite 宣称成多主机数据库。

## 先说结论

单机单进程不需要 Redis。单机多进程也可以暂时不引入 Redis，但所有进程必须共享同一个本地 SQLite 文件、同一个 Blob 根目录，并且不能把它们放在 NFS、SMB、对象存储挂载或其他无法证明文件锁语义的网络盘上。

当部署跨主机、跨 Pod、需要自动扩缩容，或者需要多实例任务队列和实时事件广播时，再切换到 PostgreSQL + Redis：

- PostgreSQL 保存任务、幂等键、审计、BOT/RAG 元数据和结果索引等事实。
- Redis 保存队列、短期租约、SSE 发布订阅和限流状态。
- Redis 丢失后可以重建实时事件；Redis 不能替代 PostgreSQL 的事实数据。
- 上传原件、RAG 原文件和大型导出仍放 Blob/对象存储，数据库只保存 URI、哈希、大小和状态。

## 目录与权限

新部署的生产单机建议：

```text
/var/lib/tc-studio/
├── tc_studio.sqlite3
├── blobs/
├── backups/
└── logs/
```

数据库父目录和 Blob 目录使用 `0700`，数据库、备份和敏感配置文件使用 `0600`。应用进程用户必须是唯一拥有这些目录读写权限的账号。

启动前设置：

```bash
export TC_STUDIO_APP_ENV=production
export TC_STUDIO_AUTH_ENABLED=true
export TC_STUDIO_DATABASE_URL=sqlite:////var/lib/tc-studio/tc_studio.sqlite3
export TC_STUDIO_CONFIG_PATH=/var/lib/tc-studio/config.json
export TC_STUDIO_DATA_DIR=/var/lib/tc-studio/data
export TC_STUDIO_STORAGE_DIR=/var/lib/tc-studio/storage
export TC_STUDIO_RESULTS_DIR=/var/lib/tc-studio/storage/batch_results
# 仅兼容旧版本 history.json；运行期事实在 SQLite，不依赖此文件
export TC_STUDIO_HISTORY_PATH=/var/lib/tc-studio/storage/history.json
```

当前 BOT/RAG 的结构化元数据已经由 SQLite 管理。Blob 根目录通过启动检查命令的
`--blob-root` 参数指定；上传原件、RAG 原文档、技能 Python 源码和大型导出仍由现有
`data/`/`storage/` 文件层管理。旧 `bot.json`、`memory.json`、`history.json`、
`history.jsonl`、`session_summary.json`、技能 JSON、`docs.json` 和 `chunks.json`
只作为迁移源或兼容回退，新写入不会创建这些结构化文件。

`storage/history.json` 只保留在旧数据观察期内。首次读取时，缺少 SQLite 行的旧
任务可以补录到 `batch_tasks`；已有 SQLite 行以数据库为准。删除任务会写入
`batch_task_tombstones`，因此旧快照不能再次创建被删除的任务。确认观察期结束后，
可以备份并归档这个文件，`TC_STUDIO_HISTORY_PATH` 仅作为回滚兼容配置保留。

高危接口规则存放在 SQLite 的 `app_settings`，修改会产生 `audit_events`。旧的
`data/security_config.json` 只做一次导入。`data/llm_config.json` 可能包含 API Key，
必须由应用和部署脚本保持 `0600`，父目录保持 `0700`；API Key 不进入普通 SQLite
设置表。

多进程部署必须让所有进程使用同一个 `TC_STUDIO_DATABASE_URL`、`TC_STUDIO_DATA_DIR`、
`TC_STUDIO_STORAGE_DIR` 和 Blob 根目录。SQLite 保存 BOT/RAG 元数据和任务事实，配置、
原始文件、RAG 原文档、技能源码以及批处理结果也必须落在同一个本机持久化卷上；
不能让不同进程各自使用容器内默认的 `data/` 目录。

`TC_STUDIO_AUTH_ENABLED=false` 只允许显式的本地开发环境使用。生产和预发布环境缺少
`TC_STUDIO_APP_ENV` 或 `TC_STUDIO_AUTH_ENABLED` 时应保持 fail-closed；旧的
`APP_ENV`、`AUTH_ENABLED` 和 `ALLOW_INSECURE_LOCAL_AUTH` 仍兼容读取。

## Linux 多实例启动方式

仓库提供了 [TC Studio systemd 模板](../../deploy/systemd/tc-studio-api@.service)。它把每个端口作为
一个独立服务实例，默认使用 `KillMode=control-group`，这样停止或重启 API 时会回收
同一服务组内的子进程；`UMask=0077`、`ProtectSystem=strict` 和 `ReadWritePaths`
把运行期写入限制在 `/var/lib/tc-studio`。旧的 `tim-api@.service` 和 `/var/lib/tim`
仍作为兼容部署边界保留，迁移时不能与新服务同时写同一个数据库。

部署方需要先创建固定应用用户、代码目录和共享运行期目录，并创建
`/etc/tc-studio/tc-studio.env`（权限 `0600`）：

```bash
useradd --system --home-dir /opt/tc-studio --shell /usr/sbin/nologin tc-studio
install -d -o tc-studio -g tc-studio -m 0755 /opt/tc-studio
install -d -o tc-studio -g tc-studio -m 0700 /var/lib/tc-studio
install -d -o tc-studio -g tc-studio -m 0700 /var/lib/tc-studio/data
install -d -o tc-studio -g tc-studio -m 0700 /var/lib/tc-studio/storage
install -d -o tc-studio -g tc-studio -m 0700 /var/lib/tc-studio/blobs
install -d -o tc-studio -g tc-studio -m 0700 /var/lib/tc-studio/backups
install -d -o tc-studio -g tc-studio -m 0700 /var/lib/tc-studio/acceptance
install -d -m 0755 /etc/tc-studio
install -m 0600 /dev/null /etc/tc-studio/tc-studio.env
```

如果已有代码目录中的 `config.json`，先复制到共享运行期目录并收紧权限：

```bash
if [ -f /opt/tc-studio/config.json ]; then
  install -o tc-studio -g tc-studio -m 0600 /opt/tc-studio/config.json /var/lib/tc-studio/config.json
else
  install -o tc-studio -g tc-studio -m 0600 /dev/null /var/lib/tc-studio/config.json
fi
```

环境文件至少应包含：

```bash
TC_STUDIO_APP_ENV=production
TC_STUDIO_AUTH_ENABLED=true
TC_STUDIO_DATABASE_URL=sqlite:////var/lib/tc-studio/tc_studio.sqlite3
TC_STUDIO_CONFIG_PATH=/var/lib/tc-studio/config.json
TC_STUDIO_DATA_DIR=/var/lib/tc-studio/data
TC_STUDIO_STORAGE_DIR=/var/lib/tc-studio/storage
TC_STUDIO_RESULTS_DIR=/var/lib/tc-studio/storage/batch_results
# 兼容旧 history.json 的可选路径，不是运行期事实源
TC_STUDIO_HISTORY_PATH=/var/lib/tc-studio/storage/history.json
```

本阶段把 OIDC/JWKS 验收后置，所以本手册的 Linux SQLite 探针会显式使用
`TC_STUDIO_APP_ENV=development`、`TC_STUDIO_AUTH_ENABLED=false` 和临时本地认证；这只用于受控的本机
存储与进程验收。服务对公网或不可信用户开放前，必须先补齐生产 OIDC 配置并单独完成
OIDC 回归验收，不能把本次本地认证结果当成身份系统验收。

将本模板安装后启动两个实例：

```bash
install -m 0644 deploy/systemd/tc-studio-api@.service /etc/systemd/system/tc-studio-api@.service
systemctl daemon-reload
systemctl enable --now tc-studio-api@8100.service tc-studio-api@8101.service
systemctl status tc-studio-api@8100.service tc-studio-api@8101.service
```

反向代理只需把同一主机的两个后端加入 upstream，例如
`127.0.0.1:8100` 和 `127.0.0.1:8101`。两个实例必须使用相同的
`TC_STUDIO_DATABASE_URL`、`TC_STUDIO_CONFIG_PATH`、`TC_STUDIO_DATA_DIR`、
`TC_STUDIO_STORAGE_DIR`、`TC_STUDIO_RESULTS_DIR` 和 `TC_STUDIO_HISTORY_PATH`；
不同数据根目录会让共享状态和幂等验收失去意义。

systemd 模板提供进程生命周期契约，但它不会替代发布前的 SQLite 检查和强故障验收。
启用服务后，必须在目标主机重新执行本手册的启动检查、强杀探针和 HTTP 双实例探针。

## 启动前检查

执行：

```bash
PYTHONPATH=. python scripts/check_sqlite_deployment.py \
  --database /var/lib/tc-studio/tc_studio.sqlite3 \
  --blob-root /var/lib/tc-studio/blobs \
  --results-root /var/lib/tc-studio/storage/batch_results \
  --backup-root /var/lib/tc-studio/backups \
  --data-root /var/lib/tc-studio/data \
  --storage-root /var/lib/tc-studio/storage \
  --min-free-bytes 10737418240
```

必须同时满足：

- `journal_mode` 为 `wal`。
- SQLite `synchronous` 为 `FULL`，避免电源/宿主机崩溃后最新幂等提交丢失。
- `foreign_keys` 为 `true`。
- `integrity` 为 `ok`。
- 数据库和父目录权限符合 `0600/0700`。
- 如果存在 `tc_studio.sqlite3-wal` 或 `tc_studio.sqlite3-shm`，它们的权限也必须符合 `0600`。
- 结果目录、备份目录符合 `0700`；已有备份文件符合 `0600`。
- `TC_STUDIO_DATA_DIR` 和 `TC_STUDIO_STORAGE_DIR` 符合 `0700`，并与 SQLite 位于同一台主机的本地存储。
- 数据库和 Blob 不在网络文件系统。
- SQLite 与 Blob 位于当前主机管理的同一存储边界。
- SQLite 与结果目录位于当前主机管理的同一存储边界。
- 能识别 SQLite、Blob、结果、数据和运行期存储目录的文件系统类型；无法识别时检查直接失败。
- 磁盘剩余空间满足业务预留量。示例命令要求至少保留 10 GiB；发布系统应按实际任务大小调整 `--min-free-bytes`。

检查脚本只证明当前挂载、权限和 SQLite 状态；它不能证明容器被强杀后的恢复、真实磁盘耗尽行为或备份介质可用性。

## 双进程验收

发布到单机多进程环境后，必须在目标 Linux 主机上运行一次独立进程验收：

```bash
PYTHONPATH=. python scripts/acceptance_sqlite_multiprocess.py \
  --work-dir /var/lib/tc-studio/acceptance/sqlite-multiprocess \
  --timeout 60
```

这个脚本会启动多个独立 Python 解释器，全部指向同一个 SQLite 文件和结果目录，
并验证：

- 相同 `Idempotency-Key` 并发提交只生成一个任务。
- 一个进程创建任务，另一个进程执行并读取最终状态。
- 另一个进程可以暂停、恢复和终止 Worker。
- 任务事件可以跨进程从 SQLite 回放。
- 过期租约会进入 `UNKNOWN`。
- 旧 Worker 不能把 `UNKNOWN` 任务写回 `COMPLETED`。
- JSONL 结果和 SQLite 结果索引保持一致。

脚本只使用 `dry_run` 任务，不会调用腾讯云。它证明的是当前主机上的 SQLite
文件锁、初始化、租约和跨进程状态边界；它不能证明真实腾讯云请求的副作用、
OIDC 身份系统、容器强杀或多主机故障转移。

验收通过后再进行真实外部 API、OIDC 和沙箱验收。双进程脚本失败时应保留
`--work-dir` 目录，检查其中的 SQLite、WAL、结果文件和子进程错误输出。

为了完成本阶段的单机生产就绪验收，还必须执行一次真实 OS 进程强杀和文件故障探针：

```bash
PYTHONPATH=. python scripts/acceptance_sqlite_multiprocess.py \
  --work-dir /var/lib/tc-studio/acceptance/sqlite-hard-faults \
  --timeout 60 \
  --hard-kill \
  --file-faults \
  --hard-kill-lease-seconds 5
```

还要通过真实 HTTP 实例验收。它会启动两个独立的 Uvicorn 进程，各自监听一个本地
端口，但共享同一个 SQLite 和文件根目录，然后通过 HTTP 并发提交同一个
`Idempotency-Key`：

```bash
PYTHONPATH=. python scripts/acceptance_http_multiprocess.py \
  --work-dir /var/lib/tc-studio/acceptance/http-multiprocess \
  --timeout 60
```

这个检查证明的是两个实际 API 进程都能启动、共享任务事实、形成一个新建请求和一个
幂等重放，并且两个进程都能读到 `COMPLETED`。它使用开发模式本地认证和 `dry_run`
任务，不代表 OIDC 或腾讯云真实副作用验收。

最近一次 Linux 验收记录见
[Linux 单机多进程验收记录](linux-single-host-acceptance-2026-11-07.md)。该记录包含
容器版本、降权用户、启动检查、真实 `SIGKILL` 和双 HTTP 实例的实测结果。

这次探针会启动真实 Worker 进程并发送 `SIGKILL`，由另一个进程等待租约过期、
读取 `UNKNOWN`，再用被杀 Worker 的旧 `worker_id` 尝试终态写入。它还会验证结果
文件截断、同长度内容篡改、删除、权限不可读和只读 SQLite 的拒绝路径。强杀探针
使用 5 秒租约只是为了把验收时间限制在可控范围；生产默认租约仍按代码配置执行，
验收结果不代表腾讯云外部副作用已经自动重试。

幂等记录的保留策略必须遵守同一边界：`COMPLETED`、`FAILED`、`CANCELLED` 可以在保留期
后由后台清理，`RESERVED`、`RUNNING`、`PAUSED` 和 `UNKNOWN` 不得因为时间经过而删除。
其中 `UNKNOWN` 表示外部副作用结果无法确认，重新使用同一个幂等键必须返回原任务并等待人工核对。

在没有 Linux 主机可直接操作时，可以使用临时 Linux 容器完成同一套代码验收。容器
不得挂入生产数据库、真实 `config.json` 或密钥；验收数据库和结果文件放在容器临时
目录中：

```bash
docker run --rm \
  -v "$PWD":/workspace \
  -w /workspace \
  python:3.13-slim \
  sh -lc '
    python -m pip install --disable-pip-version-check --require-hashes -r requirements.lock &&
    useradd --create-home --uid 1000 tc-studio-acceptance &&
    mkdir -p /tmp/tc-studio-acceptance &&
    chown -R 1000:1000 /tmp/tc-studio-acceptance &&
    su -s /bin/sh tc-studio-acceptance -c \
      "PYTHONPATH=. python scripts/acceptance_sqlite_multiprocess.py \
       --work-dir /tmp/tc-studio-acceptance \
       --timeout 60 \
       --hard-kill \
       --file-faults \
       --hard-kill-lease-seconds 5"
  '
```

容器命令只证明 Linux 用户态下的真实独立进程、SQLite 锁和本地文件权限语义；
发布前仍必须在实际 `/var/lib/tc-studio` 挂载点执行启动检查和一次同样的探针。

如果要在容器中运行全量回归，必须安装开发锁文件，并使用非 root 用户运行测试。
否则 root 可以读取 `chmod 000` 的文件，导致文件权限测试出现环境假通过：

```bash
docker run --rm \
  -v "$PWD":/workspace \
  -w /workspace \
  python:3.13-slim \
  sh -lc '
    python -m pip install --disable-pip-version-check --require-hashes -r requirements-dev.lock &&
    useradd --create-home --uid 1000 tc-studio-test &&
    test_root=$(mktemp -d /tmp/tc-studio-test.XXXXXX) &&
    mkdir -p "$test_root/repo" &&
    tar --exclude=.git --exclude=.venv --exclude=.pytest_cache --exclude=__pycache__ \
      -cf - -C /workspace . | tar -xf - -C "$test_root/repo" &&
    chown -R tc-studio-test:tc-studio-test "$test_root" &&
    su -s /bin/sh tc-studio-test -c \
      "cd $test_root/repo && PYTHONPATH=. python -m pytest -q"
  '
```

`requirements-dev.lock` 包含 `pytest`、`pytest-asyncio` 和开发测试依赖，不能用生产
锁文件代替。当前 Docker Desktop Linux/arm64 复验已经通过 `283 passed`；如果生产主机
是 x86_64 或其他架构，仍需在目标架构上重新安装锁文件并复跑。

## 备份与恢复

在线备份使用 SQLite backup API，不直接复制正在写入的 WAL 文件：

```bash
PYTHONPATH=. python scripts/backup_sqlite.py \
  --database /var/lib/tc-studio/tc_studio.sqlite3 \
  --output /var/lib/tc-studio/backups/tc-studio-before-release.sqlite3
```

备份脚本会执行完整性检查并将备份权限收紧到 `0600`。备份文件必须复制到独立介质或对象存储，并至少保留一份异机副本。

恢复前应停止所有 TC Studio 进程（包括旧 TIM 兼容服务），确认没有其他进程打开目标数据库，然后执行：

```bash
PYTHONPATH=. python scripts/restore_sqlite.py \
  /var/lib/tc-studio/backups/tc-studio-before-release.sqlite3 \
  --database /var/lib/tc-studio/tc_studio.sqlite3 \
  --force
```

恢复后必须重新执行启动前检查，并抽查：

- 最近批处理的状态、进度和 `UNKNOWN` 任务。
- `task_events` 是否可以回放。
- RAG 文档数量、文件 URI 和文件实际存在性。
- 审计事件是否仍能按租户和操作者查询。

## JSON 到 SQLite 的迁移

迁移命令默认只读扫描，不修改文件：

```bash
PYTHONPATH=. python scripts/migrate_json_metadata_to_sqlite.py \
  --data-root /var/lib/tc-studio/data \
  --database /var/lib/tc-studio/tc_studio.sqlite3
```

确认扫描报告中的数量、无异常文件后，先完成数据库备份，再执行追加导入：

```bash
PYTHONPATH=. python scripts/migrate_json_metadata_to_sqlite.py \
  --data-root /var/lib/tc-studio/data \
  --database /var/lib/tc-studio/tc_studio.sqlite3 \
  --apply
```

迁移具备以下边界：

- 原始 JSON、JSONL、RAG 原文件和技能 Python 文件保留，便于回滚和双读比对。
- BOT 定义、owner、visibility、记忆、会话摘要、技能元数据、会话消息、RAG 文档和分块索引写入 SQLite。
- 技能代码只写入 `code_uri` 和 `code_sha256`，代码正文继续在文件层。
- `model_override` 等定义中的密钥字段会在写入 SQLite 前脱敏。
- `data/bots/<bot>/users/<hash>` 无法从哈希反推出真实 subject。没有显式映射时必须跳过，不能把它归给所有用户。
- 迁移报告中的 `rag_document_id_collision_groups` 表示历史源文件存在重复文档 ID；
  重复记录会通过稳定的 `~legacy-<hash>` 后缀保留为独立数据库行，不会互相覆盖。
- 迁移报告中的 `rag_chunk_index_collisions` 表示切片序号重复或非法；
  SQLite 会分配未使用的序号，并在元数据中保留原始值。
- `ambiguous_rag_documents` 非空时，说明切片无法唯一匹配到重复文档；
  这批文档可以进入数据库做人工核对，但在清单清零前不能切换 RAG 的正式读写路径。
- 显式映射文件示例：

```json
{
  "public_bot/users/8d6f...": {
    "issuer": "https://issuer.example",
    "tenant_id": "tenant-a",
    "subject": "user-123"
  }
}
```

对应执行：

```bash
PYTHONPATH=. python scripts/migrate_json_metadata_to_sqlite.py \
  --data-root /var/lib/tc-studio/data \
  --database /var/lib/tc-studio/tc_studio.sqlite3 \
  --partition-map /var/lib/tc-studio/partition-map.json \
  --apply
```

当前版本已经完成 BOT/RAG 结构化元数据的 SQLite 读写切换。迁移后使用数据库数量、
ID、哈希和文件存在性做一次核验；SQLite 行存在时运行期不会再以旧 JSON 为事实源。
旧文件保留用于回滚和兼容，发现没有 SQLite 行时才允许触发一次导入。

## 任务状态与故障处理

批处理和目录同步都使用租约：

| 状态 | 处理 |
| --- | --- |
| `RESERVED/PENDING` | 可以由同一幂等请求恢复。 |
| `RUNNING` | 持有有效租约的 Worker 执行。 |
| `PAUSED` | 等待显式恢复。 |
| `COMPLETED/FAILED/CANCELLED` | 终态，旧 Worker 不得覆盖。同步任务取消也会进入 `CANCELLED`。 |
| `UNKNOWN` | 租约过期或执行权丢失，外部副作用无法确认，禁止自动重试。 |

`UNKNOWN` 是有意的安全边界。腾讯云请求可能已经成功，但进程可能在收到响应前退出；自动重试会把一次不确定请求变成重复写操作。人工核对完成后，再由业务方决定是否重新创建任务。

文档批量同步的正文仍保存在
`${TC_STUDIO_STORAGE_DIR}/docs/*.json` 文件缓存中，写入使用临时文件、
`fsync`、`os.replace` 和跨进程锁；没有运行期文件时，程序只读使用发布包内的
`storage/docs/*.json` fallback。目录同步写入
`${TC_STUDIO_STORAGE_DIR}/catalogs/*_full_catalog.json`，公共规范写入
`${TC_STUDIO_STORAGE_DIR}/*_common_specs.json` 和 `*_data_types.json`，不会修改
`/opt/tc-studio/core`。任务句柄、租约、进度和取消标志保存在 `sync_jobs`。
因此两个 API 进程查询同一个同步任务会看到同一状态，目录注册器和公共规范缓存也会
在发现运行期文件版本变化后刷新。

## 发布验收矩阵

| 验收项 | 本地自动化能证明什么 | 仍需在线上验证什么 |
| --- | --- | --- |
| SQLite WAL、外键、权限 | 连接配置、schema 和权限检查有效 | 真实挂载点重启后的 WAL 恢复 |
| 单进程任务 | 任务预留、进度、终态和事件回放 | 真实腾讯云请求的超时和副作用不确定性 |
| 多进程任务 | 共享文件下的事务和租约逻辑 | 目标主机的文件锁、进程调度和资源压力 |
| 备份恢复 | backup API 和 integrity_check | 备份介质、异机恢复和恢复时间目标 |
| 租户隔离 | issuer、tenant、subject 范围的回归测试 | 真实 OIDC issuer、JWKS、租户 claim 约定 |
| Blob 结果索引 | JSONL 与 SQLite 索引数量、偏移、长度、哈希；丢失/截断/篡改/不可读时拒绝导出 | 真实磁盘耗尽、卷挂载故障和清理任务在真实卷上的行为 |
| Worker 强杀 | 真实 OS 进程 `SIGKILL` 后由其他进程收敛到 `UNKNOWN`，旧 worker 不能回写 | 真实生产进程管理器、内核资源压力和外部云请求结果核对 |
| Agent 沙箱 | 本地测试的 AST、RPC 和资源边界 | Linux nsjail、容器强杀、内核和 cgroup 隔离 |
| Redis/PostgreSQL 演进 | 当前接口边界和 ADR | 真正的多主机故障转移、队列重复投递和发布订阅丢失 |

没有真实环境证据的项目必须标记为“未验证”，不能用本地 `pytest` 结果替代生产验收。

## 依赖锁定与可重复构建

生产环境安装入口是：

```bash
./scripts/install_locked.sh
```

它执行以下不可改写锁文件的步骤：

```bash
uv sync --locked --no-dev
uv pip check --python .venv/bin/python
uv run --locked --no-dev python -m compileall -q core apis scripts
```

没有 `uv` 时使用带 hashes 的 pip 锁文件：

```bash
python -m pip install --require-hashes -r requirements.lock
```

发布前至少执行一次双环境一致性检查，并比较两个环境的包版本集合：

```bash
uv lock --check
UV_PROJECT_ENVIRONMENT=/tmp/tc-studio-lock-a uv sync --locked --no-dev
UV_PROJECT_ENVIRONMENT=/tmp/tc-studio-lock-b uv sync --locked --no-dev
uv pip freeze --python /tmp/tc-studio-lock-a/bin/python
uv pip freeze --python /tmp/tc-studio-lock-b/bin/python
```

`requirements.txt` 使用范围版本，只用于兼容安装和旧环境过渡，不能作为生产
可重复构建入口。
