# ADR-0002：TC Studio 命名空间迁移与 TIM 兼容窗口

- 状态：已接受
- 日期：2026-11-08
- 范围：应用运行时命名、环境变量、单机 SQLite 默认文件和 systemd 部署路径

## 背景

产品品牌已经是 **TC Studio**，但运行时仍有一组历史 `TIM_*` 环境变量、
`tim.sqlite3` 文件名以及 `/var/lib/tim`、`/etc/tim`、`/opt/tim` 部署路径。
其中 `TIM` 还是真实的腾讯云即时通信 IM 服务标识，不能把业务标识、API
目录和服务标签一起改名。

这次迁移的目标是让 TC Studio 成为应用自身命名的唯一新标准，同时保证已经
运行的单机实例可以继续启动、继续读取已有凭据和 SQLite 数据。

## 决策

### 1. 新命名空间

应用自有运行配置统一使用 `TC_STUDIO_*` 前缀：

| 旧名称 | 新名称 |
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
| `APP_ENV` | `TC_STUDIO_APP_ENV` |
| `AUTH_ENABLED` | `TC_STUDIO_AUTH_ENABLED` |
| `ALLOW_INSECURE_LOCAL_AUTH` | `TC_STUDIO_ALLOW_INSECURE_LOCAL_AUTH` |
| `CORS_ALLOWED_ORIGINS` | `TC_STUDIO_CORS_ALLOWED_ORIGINS` |
| `ALLOW_PRIVATE_MODEL_HOSTS` | `TC_STUDIO_ALLOW_PRIVATE_MODEL_HOSTS` |
| `LOCAL_MODEL_ALLOWED_PORTS` | `TC_STUDIO_LOCAL_MODEL_ALLOWED_PORTS` |

新变量存在时始终优先。旧变量只作为读取兼容，不会被程序自动写回、覆盖或
转换；新旧同时存在且值不同，程序使用新值并记录不含配置值的告警。

`TIM_*` 只有在这里表示应用运行配置时才属于旧命名。`TIM` 作为即时通信
IM 的业务代码、接口标签、域名 `console.tim.qq.com` 和目录文件名仍然保留。

### 2. SQLite 文件名

全新安装的默认主库名改为：

```text
storage/tc_studio.sqlite3
```

如果默认位置只有旧的 `storage/tim.sqlite3`，应用继续使用旧文件并记录迁移
提示。若新旧文件同时存在，应用只使用 `tc_studio.sqlite3`，不会自动合并、
覆盖或删除 `tim.sqlite3`。这是为了避免两个 SQLite 文件同时写入造成事实分叉。

指定了 `TC_STUDIO_DATABASE_URL` 或 `TC_STUDIO_SQLITE_PATH` 时，显式路径优先；
对应的旧 `TIM_*` 变量仍然可用。

### 3. 部署路径

新部署使用：

```text
/opt/tc-studio
/var/lib/tc-studio
/etc/tc-studio/tc-studio.env
tc-studio-api@.service
```

旧的 `tim-api@.service`、`/opt/tim`、`/var/lib/tim` 和 `/etc/tim/tim.env`
保留为兼容部署边界。旧服务不应与新服务同时指向同一个 SQLite 文件，迁移时
应先停旧服务、备份并验真，再切换到新服务。

## 不采用的方案

- 不在启动时自动复制或重命名 SQLite。自动复制会产生两个看似都有效的主库，
  之后的写入无法合并。
- 不把所有包含 `TIM` 的代码标识符改成 `TC_STUDIO`。即时通信 IM 的 API、
  `TIM` 标签和腾讯云域名是业务事实，不是应用命名空间。
- 不把 OIDC 的 `aud` 值自动从 `tim-studio` 改成 `tc-studio`。Audience 是
  身份提供方和部署方共同约定的安全配置，必须单独变更并重新验签。

## 验证要求

- 新变量单独设置时能启动并使用指定路径。
- 只有旧变量时仍能启动，并产生迁移告警。
- 新旧变量冲突时新变量胜出，日志不包含密钥值。
- 只有旧 SQLite 文件时不会创建第二个主库。
- 新旧 SQLite 同时存在时不会自动合并。
- 新 systemd 模板使用 TC Studio 路径，旧模板仍保持旧路径兼容。

## 迁移窗口

本兼容窗口至少覆盖一个完整发布周期。实际删除 `TIM_*` 读取和旧部署模板
前，必须先检查生产环境变量、systemd unit、容器编排文件和备份脚本，并完成
一次可恢复演练。
