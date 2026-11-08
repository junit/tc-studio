# TC Studio 命名空间迁移操作说明

这份说明处理的是应用自己的命名空间迁移。腾讯云即时通信 IM 仍然简称
`TIM`，`console.tim.qq.com`、IM API 标签和 IM 目录数据不需要改名。

## 兼容规则

配置读取顺序固定为：

1. `TC_STUDIO_*` 新变量；
2. 对应的 `TIM_*` 旧变量；
3. 历史无前缀别名（目前只有 `ALLOW_UNCONFINED_CODE_EXECUTOR`）；
4. 代码默认值。

新变量和旧变量都存在时，新变量生效。两者值不一致时会告警，但日志只打印
变量名，不打印路径以外的敏感值。程序不会把旧变量写回环境，也不会静默修改
旧配置文件。

## 变量对照表

| 旧变量 | 新变量 |
| --- | --- |
| `TIM_DATABASE_URL` | `TC_STUDIO_DATABASE_URL` |
| `TIM_SQLITE_PATH` | `TC_STUDIO_SQLITE_PATH` |
| `TIM_CONFIG_PATH` | `TC_STUDIO_CONFIG_PATH` |
| `TIM_DATA_DIR` | `TC_STUDIO_DATA_DIR` |
| `TIM_STORAGE_DIR` | `TC_STUDIO_STORAGE_DIR` |
| `TIM_RESULTS_DIR` | `TC_STUDIO_RESULTS_DIR` |
| `TIM_HISTORY_PATH` | `TC_STUDIO_HISTORY_PATH` |
| `TIM_CODE_EXECUTOR_MODE` | `TC_STUDIO_CODE_EXECUTOR_MODE` |
| `TIM_NSJAIL_BIN` | `TC_STUDIO_NSJAIL_BIN` |
| `TIM_NSJAIL_ROOT` | `TC_STUDIO_NSJAIL_ROOT` |
| `TIM_NSJAIL_PYTHON` | `TC_STUDIO_NSJAIL_PYTHON` |
| `TIM_NSJAIL_SECCOMP_POLICY` | `TC_STUDIO_NSJAIL_SECCOMP_POLICY` |
| `TIM_NSJAIL_RLIMIT_AS_MB` | `TC_STUDIO_NSJAIL_RLIMIT_AS_MB` |
| `TIM_NSJAIL_RLIMIT_NOFILE` | `TC_STUDIO_NSJAIL_RLIMIT_NOFILE` |
| `TIM_NSJAIL_RLIMIT_NPROC` | `TC_STUDIO_NSJAIL_RLIMIT_NPROC` |
| `TIM_SDKAPPID` | `TC_STUDIO_SDKAPPID` |
| `TIM_SECRET_KEY` | `TC_STUDIO_SECRET_KEY` |
| `TIM_USERSIG` | `TC_STUDIO_USERSIG` |
| `ALLOW_UNCONFINED_CODE_EXECUTOR` | `TC_STUDIO_ALLOW_UNCONFINED_CODE_EXECUTOR` |

## 新单机部署

新部署建议使用以下目录：

```text
/opt/tc-studio
/var/lib/tc-studio/
├── tc_studio.sqlite3
├── config.json
├── data/
├── storage/
├── blobs/
├── backups/
└── acceptance/
/etc/tc-studio/tc-studio.env
```

环境文件示例：

```bash
APP_ENV=production
AUTH_ENABLED=true
TC_STUDIO_DATABASE_URL=sqlite:////var/lib/tc-studio/tc_studio.sqlite3
TC_STUDIO_CONFIG_PATH=/var/lib/tc-studio/config.json
TC_STUDIO_DATA_DIR=/var/lib/tc-studio/data
TC_STUDIO_STORAGE_DIR=/var/lib/tc-studio/storage
TC_STUDIO_RESULTS_DIR=/var/lib/tc-studio/storage/batch_results
TC_STUDIO_HISTORY_PATH=/var/lib/tc-studio/storage/history.json
TC_STUDIO_NSJAIL_BIN=/usr/local/bin/nsjail
TC_STUDIO_NSJAIL_ROOT=/opt/tc-studio/nsjail-rootfs
TC_STUDIO_NSJAIL_PYTHON=/usr/bin/python3
TC_STUDIO_NSJAIL_SECCOMP_POLICY=/opt/tc-studio/nsjail-python.policy
TC_STUDIO_CODE_EXECUTOR_MODE=nsjail
```

部署时安装 `deploy/systemd/tc-studio-api@.service`。两个实例必须共享同一套
`TC_STUDIO_DATABASE_URL`、`TC_STUDIO_CONFIG_PATH`、`TC_STUDIO_DATA_DIR`、
`TC_STUDIO_STORAGE_DIR` 和 `TC_STUDIO_RESULTS_DIR`。

## 旧部署切换

旧环境可以继续使用：

```bash
TIM_DATABASE_URL=sqlite:////var/lib/tim/tim.sqlite3
TIM_CONFIG_PATH=/var/lib/tim/config.json
TIM_DATA_DIR=/var/lib/tim/data
TIM_STORAGE_DIR=/var/lib/tim/storage
TIM_RESULTS_DIR=/var/lib/tim/storage/batch_results
```

推荐迁移步骤：

1. 停止旧的 `tim-api@*.service` 实例，避免迁移时继续写入。
2. 使用现有备份脚本生成并校验 `tim.sqlite3` 的 SQLite 备份。
3. 将备份恢复到 `/var/lib/tc-studio/tc_studio.sqlite3`，恢复后再次执行
   `PRAGMA integrity_check` 和部署检查。
4. 将 `config.json`、`data/`、`storage/` 和 Blob 文件复制到新的运行目录，
   保持父目录 `0700`、配置和数据库 `0600`。
5. 使用全量 `TC_STUDIO_*` 环境变量启动新 systemd 模板。
6. 运行 SQLite 多进程和 HTTP 多实例验收，确认幂等、任务状态、BOT/RAG 元数据
   和结果文件均可读取。
7. 保留旧目录和备份，观察一个完整发布周期后再归档；不要在新旧服务同时运行
   时让它们写同一份 SQLite 文件。

应用只会在默认路径发现“旧库存在、新库不存在”时继续使用旧库。显式设置新库
路径后，应用不会自动把旧库内容合并进新库。

如果要把默认旧库复制到新命名空间，使用显式迁移命令。命令默认只做 dry-run，
不会创建目标文件：

```bash
PYTHONPATH=. python scripts/migrate_tim_namespace.py \
  --source /var/lib/tim/tim.sqlite3 \
  --target /var/lib/tc-studio/tc_studio.sqlite3
```

确认已停止旧服务、完成备份并检查 dry-run 输出后，再执行复制：

```bash
PYTHONPATH=. python scripts/migrate_tim_namespace.py \
  --source /var/lib/tim/tim.sqlite3 \
  --target /var/lib/tc-studio/tc_studio.sqlite3 \
  --execute
```

目标文件已经存在时，命令会拒绝覆盖；只有确认要用旧库替换目标库时才使用
`--force`。源库始终保留，迁移过程会在替换前后执行 SQLite 完整性检查，并将目标
文件权限收紧为 `0600`。

## 回滚

回滚时停止新 `tc-studio-api@*.service`，恢复旧环境文件和旧 `tim-api@*.service`，
然后让旧服务指向迁移前备份或经验证的旧库。回滚前后都要保存验收输出，避免把
新旧两次运行产生的写入误认为可以自动合并。
