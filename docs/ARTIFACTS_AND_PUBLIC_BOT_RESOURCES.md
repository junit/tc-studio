# 文件产出、公共 BOT 与本地身份边界

本次检查针对 2026-11-12 报告生成失败后的五个实际问题。目标仍是单机多进程，
文件元数据使用 SQLite，二进制文件保留在本机受控目录；无需为这些修复引入 Redis。

## 五个问题的实证结论

| 问题 | 代码及运行证据 | 结论与修复 |
|---|---|---|
| 同名文件是否覆盖 | 原生成函数以展示名称构造目标路径，最后 `os.replace`；第一轮 12B PDF 已被第二轮覆盖 | 【确凿证实】旧下载会变成新内容。正式发布改用 `g_<uuid>_<名称>`，不覆盖已有目标，展示名称保持可读 |
| 文件中心为什么没有产出物 | 本地公共 BOT 有 SQLite 上传记录，但生成函数没有调用 `register_bot_file`；原列表只要存在任意数据库行就不再扫描磁盘。本地 HTTP 文件列表实际返回空数组 | 【确凿证实】是登记链路遗漏，与公共 BOT 无关。新发布同时登记名称、用户范围、大小、哈希和 Blob 地址；兼容登记有效旧文件 |
| PDF 以外的格式是否可信 | 原保存逻辑会把 dict/list 直接写成 JSON，未区分 `.xlsx`、`.docx`、`.csv`；二进制 RPC 摘要还会影响其他二进制格式 | 【确凿证实】仅看后缀不能证明格式有效。增加真实 CSV/XLSX/DOCX 编码及 Office/ZIP/图片/PDF 校验；JSON 必须可解析 |
| BIN、TXT 探测文件为什么也有下载 | 原逻辑通过目录变化发现产出物，并把未写在最终答案中的文件链接自动追加 | 【确凿证实】把调试过程当成交付。独立临时工作区与显式发布替代目录扫描；探测文件默认不产生下载卡片 |
| `open` 是否能在真实沙箱中读写 | 新补充的真实 spawn/RPC 用例确定性复现 `_QuotaFile` 无法通过 IPC 传输，原有仅在进程内调用的文件测试没有覆盖此路径 | 【确凿证实】新增受限文件代理，宿主文件对象留在服务端，通过当前执行器的随机句柄访问；二进制与文本都经同一有界传输协议 |
| 本地所有权迁移 subject 填什么 | 当前运行的 `/api/auth/me` 返回 subject `local`，角色 admin/operator；关闭认证分支固定返回该身份 | 本地私有旧 BOT 填 `local`，设置面板新增当前身份与“使用当前身份”。保持此前空 owner 范围内的历史、记忆、文件与准则可读 |
| 公共 BOT 是否支持个人资源 | 原历史/文件/记忆已有 requester scope，知识库却被路由、提示词和工具安装共同禁用；原知识库文件目录未按公共 BOT 用户分开 | 已支持个人知识库：路由、SQL 范围、Blob 目录、检索工具、系统提示词全部携带当前用户身份 |
| `subject=local` 是否总是本地开发 | 原目录判断只看 subject 字符串；真实认证 subject 恰为 `local` 时可能命中开发根目录 | 【确凿证实，代码路径】实际利用需开启认证且 subject 恰为 `local`，当前开发服务关闭认证。修改为同时检查 issuer 和 tenant，回归验证带认证来源的 `local` 无法访问开发根目录 |

修复版 PDF 位于仓库 `output/pdf/` 时，是独立排版验收产物，不会自动进入产品文件中心。
这与 BOT 会话生成的文件没有登记，是两条不同链路。原始报告保留作故障证据。

## 正式文件与临时文件

临时文件可以反复写入、检查或覆盖；它们不会提供下载 URL，会话结束会清理。
正式发布的文件使用独立存储名和稳定链接，修改内容需要发布新版本。

```python
# 调试与中间数据，不对用户交付。
scratch = save_file("probe.txt", "intermediate data")

# 正式表格：服务端创建真正的 XLSX，并登记到当前用户的文件中心。
report = save_file(
    "巡检结果.xlsx",
    [{"流名": "stream-a", "状态": "在线", "帧率": 25}],
    publish=True,
)

# 先保存工作文件，再确认发布。
save_file("诊断记录.md", "# 结论\n已有诊断内容。")
notes = publish_file("诊断记录.md")

# PDF helper 本身即是正式发布操作。
pdf = create_pdf_report("诊断报告.pdf", "诊断报告", "## 结论\n已有诊断内容。")
final_answer(f"报告已生成：[下载 PDF]({pdf['download_url']})")
```

必须使用工具返回的 `download_url`，不能用展示名称拼接地址。
文件名含中文、空格、`#`、`?` 时，发布结果会正确编码 URL。
精确存储名优先于原始展示名称的兼容匹配，防止后来创建的同名别名改变旧链接。

| 格式 | 推荐输入 | 生成与校验范围 |
|---|---|---|
| TXT / Markdown / LOG | UTF-8 文本 | 原文保存；空正式文件拒绝 |
| JSON | dict、list，或合法 JSON 文本 | 真正的 JSON 编码；非法 JSON、NaN、Infinity 明确拒绝 |
| CSV | 对象列表、二维列表，或 UTF-8 CSV | 实际 CSV 编码，UTF-8 BOM；不可信公式前缀按文本处理，数值负数保留 |
| XLSX | 对象列表或二维列表 | openpyxl 生成并回读；外部字符串单元格固定为文本，不执行公式 |
| DOCX | 文本或简单 Markdown | python-docx 生成标题与段落并回读；并非完整 Markdown 富排版转换器 |
| PDF | `create_pdf_report` 的标题和 Markdown 正文 | 字体、换行和分页由服务端负责；任意 PDF 仍需结构及视觉分别验收 |
| ZIP / PPTX / 图片 | 已生成的真实格式 bytes | 校验 ZIP 成员数/解压容量/压缩比、Office 主体或图片格式；没有承诺从 dict 自动生成 PPTX |
| XLS / BIN | 不作为正式生成格式 | XLS 明确要求转用 XLSX；BIN 探测数据留在临时工作区 |

表格上限为 20000 行、200 列、500000 单元格，单元格最多 32767 字符；DOCX 正文最多
200000 字符。超过限制明确失败，不静默截断。保留生成文件单件 100 MiB、总数 1000、
总量 512 MiB 的配额；Office 和 PDF 还受各自解析上限约束。
二进制经 RPC 传入时仍受 8 MiB 帧限制，含 base64 开销；服务端 PDF/Office 编码避免传输大块编码结果。

发布过程先预留 SQLite 文件名额，完整写入临时文件并校验，再原子创建独立目标，登记哈希后返回成功。
同一主机多个进程使用文件锁与 SQLite 预留记录协调。登记失败会删除新文件并释放预留；
进程异常中止留下的未登记 `g_*` 文件不会被列表、下载或旧文件兼容扫描自动提升为正式产物。

`open` 只在受控工作区写入，读取也只能在当前用户的工作区和上传目录中进行。真实执行器通过
受限 RPC 文件代理支持上下文管理、文本/二进制读写、逐行读取、seek/tell/flush/close；
不传输 Python 文件对象，也不提供 fileno、buffer、raw、detach 等宿主访问入口。
每个执行器最多 32 个打开文件、10000 次文件操作，单次读取最多 512 Ki 字节或字符，
累计读取最多 64 Mi 字节或字符；超限需分块或缩小任务。关闭执行器会关闭遗漏的句柄并清除工作区配额缓存。
spawn 和 nsjail 的 RPC 代理共用此边界；socket 回归证明协议可用，不能替代 Linux/nsjail 主机验收。

## 公共 BOT 的共享范围

公共 BOT 的意义是同一套助手定义可以服务多个用户，用户不必复制一份 BOT 才能开始工作。
“可以共同使用助手”不表示“共同读取这个助手处理过的所有资料”。

| 对象 | 公共 BOT 中的范围 |
|---|---|
| 名称、人设、模型配置、工具权限、共享准则 | BOT 定义，由管理员管理 |
| 会话历史、记忆、上传文件、生成文件 | 当前 issuer + tenant + subject |
| 个人知识库文档、索引切片、下载、检索命中、启停状态 | 当前 issuer + tenant + subject |
| 自编技能元数据、源码、执行与删除 | 当前 issuer + tenant + subject；同名技能分区，旧公共根目录不自动继承 |
| 对话中自动学习的行为偏好 | 所有角色都只写个人记忆；共享准则需管理员显式维护 |
| 用户文档名进入提示词 | 每次请求只取当前用户可见文档，不写回公共 BOT 定义 |
| 认证前的公共 BOT 根目录和旧知识库 | 不自动分给新登录用户；需明确归属的管理流程 |

公有定义若绑定 issuer/tenant，仍只能被相同认证范围内的用户使用；内建的无范围公共 BOT
可以被不同范围引用，但租户管理员不能修改全局模板。个人资源分别隔离。管理员通过公共 BOT 的个人资源路由也只读取
当前身份的范围；这类路由不提供全用户资源浏览功能。

“通用”是业务分类，不代表 public 权限。完整设计、发现的技能/自动学习遗漏、修复差异、
浏览器验证和仍存在的跨 worker 运行态边界，见[公共 BOT 隔离审查](PUBLIC_BOT_ISOLATION_AUDIT.md)。

目录分区统一计算自 `issuer + tenant + subject` 的哈希。公共 BOT 的知识库原文件也使用
同样分区，不能只隔离 SQL 行却共享原始文件目录。SQL 的文档/切片键继续携带完整范围。

该隔离不等同于每个人拥有独立的云账号；云 API 仍使用现有平台配置的客户端，受已有角色和确认门禁约束。
暂未增加“管理员维护一份知识库并自动对所有公共 BOT 用户开放”的隐式共享模式；如将来需要，
应提供单独的显式共享资源及授权设计。

## 本地 subject 和两类迁移

关闭认证的开发环境固定身份为 `local`，所有浏览器、标签页和客户端都代表同一个开发用户。
因此换浏览器不会得到新的个人资源空间。真实多用户隔离按已验证身份实现，不提供可随意填写请求头的开发冒充接口。

```bash
curl http://127.0.0.1:8000/api/auth/me
```

- 私有历史 BOT 的“所有权迁移”设置定义的 `owner_id`。本地目标填写 `local`，可在界面直接使用当前身份。
  本地 `local` 与此前无 owner 的开发存储范围兼容，不会因这次认领突然丢失会话和记忆。
- 公共 BOT 不需要设置 `owner_id`。“旧版状态迁移”是把认证前无法归属的数据明确分配给一个真实用户；
  它不接受 `local` 作为迁移目标，不能与私有 BOT 所有权按钮混用。
- 切换为真实认证身份时，以 `/api/auth/me` 返回的 subject 为准。已有认证前资料不会自动被所有新用户继承。
  私有 BOT 定义的 owner 转移也不等于任意历史范围数据的全量自动重分配，后续正式用户迁移仍需单独验证。

## 针对性回归矩阵与验收边界

| 自动化用例 | 断言目标 |
|---|---|
| `test_common_formats_are_real_readable_and_indexed` | TXT/MD/JSON/CSV/XLSX/DOCX 可由真实解析器读取，大小与哈希和 SQLite 一致 |
| `test_common_file_helpers_roundtrip_through_real_executor_rpc` | 真实执行器传入中文结构数据，服务端生成真实 XLSX/DOCX |
| `test_scratch_open_roundtrip_through_real_executor_rpc` | 真实执行器支持 with open 文本读写，再显式发布到文件中心 |
| `test_nsjail_file_proxy_uses_bounded_socket_rpc_without_host_objects` | nsjail RPC 客户端与父执行器调度经真实 socket 传输文件代理和二进制数据 |
| `test_file_broker_rejects_raw_operations_and_resource_overflow` | 非法操作、伪造句柄、打开数量和读取/操作预算均被拒绝 |
| `test_real_executor_closes_unclosed_files_and_releases_quota_cache` | 模型遗漏 close 时，关闭执行器仍释放文件及配额缓存 |
| `test_csv_and_excel_untrusted_cells_do_not_become_formulas` | `= + - @`、前置空白/制表符不成为公式；真正的数值负数保持数字 |
| `test_mislabeled_or_invalid_formats_fail_before_publication` | JSON 冒充 Office、损坏 PDF/ZIP、非法图片、XLS/BIN 不成为已发布产物 |
| `test_table_limits_fail_without_silent_truncation` | 超长单元格、NaN/Infinity、超列数明确失败 |
| `test_scratch_probes_are_not_published_downloads_and_are_removed` | BIN/TXT/直接 open 的探测文件不出现在列表和附件，没有直接下载入口，会话关闭后删除 |
| `test_explicit_publication_uses_stable_links_and_cannot_be_overwritten` | 同名新版本保留旧内容，特殊字符 URL 可下载，正式文件不可通过工作区 open 覆盖 |
| `test_exact_storage_name_wins_over_newer_original_name_alias` | 后创建的展示名称别名不能劫持旧存储地址，删除一份不会删另一份 |
| `test_failed_sql_registration_rolls_back_bytes_and_reservation` | 元数据写入失败清理文件和预留，不返回成功附件 |
| `test_two_processes_publish_same_name_without_overwriting` | 两个真实 OS 进程发布同名文件，磁盘和 SQLite 保留两份 |
| `test_legacy_outputs_reconcile_with_existing_uploaded_rows` | 已存在上传记录时仍能兼容登记有效旧产物；探测文件和未提交的新产物不被导入 |
| `test_authenticated_subject_local_does_not_resolve_development_root` | 带认证 issuer 的 `local` 与本地开发身份分开 |
| `test_claiming_local_private_bot_retains_existing_sqlite_resources` | 本地认领旧私有 BOT 后历史、记忆、准则仍存在 |
| `test_public_bot_personal_files_history_memory_and_rag_isolate_full_http_chain` | 同一公共 BOT 下不同用户/issuer/租户的列表、下载、检索、启停、删除和提示词互不串读 |
| `test_public_knowledge_does_not_auto_inherit_pre_auth_files` | 新身份不自动获得旧公共 BOT 根知识库 |

测试在独立代码副本和数据目录执行，避免污染正在使用的资料。生产 OIDC 登录、真实模型完整任务、
Linux/nsjail 主机级隔离不由本地回归替代；本轮未扩展到多 Pod、多主机或新中间件。

2026-11-12 最终验收：

- 独立副本全量回归：`388 passed in 65.27s`。
- 文件 RPC、格式、公共 BOT 资源及鉴权等针对性回归：`98 passed in 9.88s`。
- `pnpm --dir web run typecheck`、`pnpm --dir web run build`、`uv lock --check`、
  `uv pip check`、变更模块编译及 `git diff --check` 通过。前端构建保留现有较大 bundle 提示。
- 当前开发服务 `/api/auth/me` 实测为 `subject=local`、`auth_mode=local`；
  公共 BOT 文件列表由修复前的 0 项恢复为 13 项，两份原 PDF 已登记，探测文件不在列表中。
  原 PDF 字节保持不变；列表登记不代表第二份旧 PDF 的视觉排版已经修好。
- 后端已自动重载，新构建的静态资源已由当前 HTTP 服务提供；确认包含当前身份按钮和公共 BOT 个人知识库说明。
  本轮没有操作浏览器逐项点击，也没有重新调用真实模型生成新报告。
- 原始会话未改写；上轮独立修复的 PDF 副本仍在 `output/pdf/`，没有冒充成原会话的新产物。
