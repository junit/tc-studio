# TC Studio Linux 单机多进程验收记录

## 验收范围

本记录只证明 TC Studio 在**一台 Linux 主机、一个本地持久化存储边界、多个独立进程**
下的生产基础能力。它不证明多主机、多 Pod、OIDC/JWKS、真实腾讯云副作用或 Redis
故障恢复。

文件名保留原有日期；初始记录日期为 2026-11-07，当前工作树最终复验日期为
2026-11-09。

运行环境：

- Docker Desktop Linux/arm64 容器，内核报告为 `Linux-7.0.14-linuxkit-aarch64`。
- 基础镜像 `python:3.13-slim`，实际解释器版本 `Python 3.13.18`。
- 使用 `requirements.lock` 和 `--require-hashes` 安装生产依赖。
- 验收进程使用降权用户 `tim-acceptance`，UID 1000。
- 验收目录为容器临时目录，没有挂载生产数据库、`config.json`、密钥或真实文件。

## 启动检查

执行 `scripts/check_sqlite_deployment.py`，使用 10 GiB 最低剩余空间门槛。通过条件包括：

- `journal_mode=wal`
- `synchronous=2`，即 SQLite `FULL`
- `foreign_keys=true`
- `integrity=ok`
- 数据库 `0600`、父目录 `0700`
- Blob、结果、数据、运行期存储和 SQLite 位于同一设备
- 能识别每个运行期路径的文件系统类型
- 未发现网络文件系统

本次结果：

```text
platform: Linux-7.0.14-linuxkit-aarch64-with-glibc2.41
journal_mode: wal
synchronous: 2
foreign_keys: true
integrity: ok
database_mode: 0o600
parent_mode: 0o700
same_device: true
same_device_results: true
same_device_data: true
same_device_storage: true
network_filesystem: false
filesystem_types_known: true
passed: true
```

本次检查实际使用 `--min-free-bytes 10737418240`，容器剩余空间为
`995603288064` 字节，未触发空间阈值。

## SQLite 强故障验收

执行：

```bash
PYTHONPATH=. python scripts/acceptance_sqlite_multiprocess.py \
  --work-dir /tmp/tim-acceptance-sqlite \
  --timeout 60 \
  --hard-kill \
  --file-faults \
  --hard-kill-lease-seconds 5
```

结果：

```text
concurrent_idempotency: true
cross_process_pause_resume_stop: true
real_worker_sigkill_unknown: true
sigkilled_worker_cannot_finalize: true
stale_lease_quarantine: true
stale_worker_terminal_write_rejected: true
truncated_result_rejected: true
same_length_tamper_rejected: true
missing_result_rejected: true
unreadable_result_rejected: true
readonly_database_write_rejected: true
truncated_export_409: true
missing_export_409: true
```

这组结果证明 SQLite 是跨进程事实源，Worker 被强杀后不会被旧进程伪造为成功，
结果正文和 SQLite 索引不一致时不会导出空结果或伪造成功。

## 双 HTTP 实例验收

执行：

```bash
PYTHONPATH=. python scripts/acceptance_http_multiprocess.py \
  --work-dir /tmp/tim-acceptance-http \
  --timeout 60
```

结果：

```text
two_http_processes_ready: true
concurrent_http_idempotency: true
cross_process_http_status_visibility: true
cross_process_http_completion: true
```

两个独立 Uvicorn 进程监听不同本地端口，共享同一个 SQLite 和文件根目录。相同
`Idempotency-Key` 只生成一个 `task_id`，两个进程最终都读到 `COMPLETED`。

## 备份与恢复验收

在同一 Linux 容器中创建包含审计记录的 SQLite 数据库，执行：

```bash
PYTHONPATH=. python scripts/backup_sqlite.py \
  --database /tmp/tim-backup-restore/source.sqlite3 \
  --output /tmp/tim-backup-restore/tim-backup.sqlite3
PYTHONPATH=. python scripts/restore_sqlite.py \
  /tmp/tim-backup-restore/tim-backup.sqlite3 \
  --database /tmp/tim-backup-restore/restored.sqlite3 \
  --force
```

结果：

```text
backup_integrity: ok
restore_integrity: ok
restored_audit_event: true
```

这只证明 SQLite backup API、原子替换、完整性校验和权限收紧在该 Linux 用户态环境中
生效；异机备份介质、定时任务和真实生产恢复窗口仍需要由运维系统单独验证。

## 锁文件与可重复构建验收

执行：

```bash
uv lock --check
UV_PROJECT_ENVIRONMENT=/tmp/tim-lock-a uv sync --locked --no-dev
UV_PROJECT_ENVIRONMENT=/tmp/tim-lock-b uv sync --locked --no-dev
uv pip freeze --python /tmp/tim-lock-a/bin/python
uv pip freeze --python /tmp/tim-lock-b/bin/python
python -m venv /tmp/tim-pip-lock
/tmp/tim-pip-lock/bin/python -m pip install --require-hashes --index-url https://pypi.org/simple -r requirements.lock
```

本次验证使用相同的 `requirements.lock` 安装生产依赖；两个独立 uv 环境各安装
56 个生产包，版本集合 `diff` 为空；Linux 容器和本机 pip 环境的
`--require-hashes` 安装也通过。`uv.lock` 未被安装过程改写，生产源码编译检查
和 `pip check` 均通过。

本机默认的阿里云 PyPI 镜像曾因缺少 `click==8.5.1` 导致 pip 安装失败；切换到
官方 PyPI 后安装成功。这是镜像同步不完整造成的环境问题，不是 hash 校验或锁文件
解析失败。生产部署使用内部镜像时，必须先确认镜像包含 `requirements.lock` 中的
全部版本和平台 wheel。

## 当前工作树复验

在当前工作树完成 SQLite 作用域、BOT 准则并发、路径/SSRF、输入边界、缓存、容器化
和命名空间兼容修复后，使用 Docker Desktop Linux/arm64 后端重新执行验收。Docker
镜像实际使用 Python 3.13、Node 22、`uv.lock`、`web/pnpm-lock.yaml`，运行容器以
UID 10001 非 root 用户启动；独立故障探针又以 UID 1000 的非 root 用户运行。

全量回归结果：

```text
332 passed in 63.36s
```

部署检查结果：

```text
platform: Linux-7.0.14-linuxkit-aarch64-with-glibc2.36
journal_mode: wal
synchronous: 2
foreign_keys: true
integrity: ok
database_mode: 0o600
parent_mode: 0o700
free_bytes: 992665387008
min_free_bytes: 10737418240
database_filesystem: ext4
blob_filesystem: ext4
results_filesystem: ext4
data_filesystem: ext4
storage_filesystem: ext4
same_device: true
same_device_results: true
same_device_data: true
same_device_storage: true
network_filesystem: false
filesystem_types_known: true
passed: true
```

SQLite 强故障探针结果：

```text
concurrent_idempotency: true
cross_process_pause_resume_stop: true
real_worker_sigkill_unknown: true
sigkilled_worker_cannot_finalize: true
stale_lease_quarantine: true
stale_worker_terminal_write_rejected: true
truncated_result_rejected: true
same_length_tamper_rejected: true
missing_result_rejected: true
unreadable_result_rejected: true
readonly_database_write_rejected: true
truncated_export_409: true
missing_export_409: true
```

双 HTTP 实例探针结果：

```text
two_http_processes_ready: true
concurrent_http_idempotency: true
cross_process_http_status_visibility: true
cross_process_http_completion: true
```

最终 Docker 镜像与 Compose 验收结果：

```text
docker build: passed
uv sync --frozen --no-dev: passed
pnpm install --frozen-lockfile: passed
frontend production build: passed
compose service: healthy
container user: uid=10001(tcstudio) gid=10001(tcstudio)
readonly root filesystem: true
pids limit: 512
memory limit: 2147483648
cpu limit: 2.0
/healthz: 200 {"status":"ok"}
uv pip check: 56 packages, all compatible
```

同一最终镜像下，UID 1000、只读根文件系统、可写 `/tmp` 临时文件系统的 SQLite
强故障探针也全部通过，结果与上面的 SQLite 强故障探针相同。这证明运行期不依赖
固定的 10001 UID；实际部署仍应使用专用服务账号。

当前代码的 SQLite 备份和恢复结果：

```text
backup integrity=ok
restore integrity=ok
```

本次复验还修复了一个 Linux 路径的真实边界：带外部工具能力的执行器不再使用
`fork` 将工具函数直接继承到 Worker，而是统一走 spawn/RPC；这样工具 I/O 的等待时间
能够正确从代码执行 deadline 中排除，且子进程不会直接继承父进程的能力对象。

## 仍然不属于本次证据

- `UNKNOWN` 只表示外部副作用结果未知，不会自动重试；需要人工核对后重新发起。
- BOT/RAG 结构化元数据的运行期读写已经 SQLite-first；旧 JSON/JSONL 只作为迁移源
  或缺少 SQLite 行时的兼容回退，新写入不会创建旧结构化文件。
- RAG 文档使用内部 `document_key`，并以 `document_id + bot_id + issuer + tenant_id +
  owner_subject` 组成作用域唯一边界；BOT 准则使用独立的 `bot_guidelines` 表。
- 上传原件、RAG 原文档、技能 Python 源码和大结果正文仍在本地 Blob/文件层；
  SQLite 保存归属、状态、哈希、大小、URI 和索引。
- 大结果正文仍在本地 JSONL/Blob，SQLite 保存索引、哈希和偏移。
- 批处理事实和事件可以跨进程共享；AgentRun、审批等待、`/chat` 对话上下文、
  `/chat/stream` 实时连接状态和 sandbox 运行句柄仍在进程内。多实例部署必须把同一
  Agent 会话、审批回调和流式连接固定到同一 API 实例。
- 没有执行真实腾讯云写操作、OIDC/JWKS、真实生产进程管理器、设备级磁盘耗尽、
  nsjail/cgroup 隔离、多主机或多 Pod 验收。
- OIDC、PostgreSQL、Redis、多 Pod、多主机以及真实外部 API 副作用仍未验收。
