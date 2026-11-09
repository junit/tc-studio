# 腾讯云 API 3.0 业务标准化接入与生态集成规范指南

> **版本**：v1.0.0  
> **更新时间**：2026-10-30  
> **参考实现**：腾讯云直播 (CSS) 261 项 API 3.0 全量接入  
> **适用范围**：TRTC 实时音视频、VOD 云点播、COS 对象存储、CVM 云服务器、VPC 私有网络及后续任意腾讯云 API 3.0 标准化产品。

---

## 目录
1. [接入背景与核心设计原则](#1-接入背景与核心设计原则)
2. [总体架构与流水线全景图](#2-总体架构与流水线全景图)
3. [阶段一：官方元数据与文档自动化同步 (Ingestion)](#3-阶段一官方元数据与文档自动化同步-ingestion)
4. [阶段二：结构化数据模型与存储规范 (Schema & Storage)](#4-阶段二结构化数据模型与存储规范-schema--storage)
5. [阶段三：核心通信与 TC3 签名驱动 (Signing & Dispatcher)](#5-阶段三核心通信与-tc3-签名驱动-signing--dispatcher)
6. [阶段四：注册中心动态发现与 SSoT 自适应 (Registry & SSoT)](#6-阶段四注册中心动态发现与-ssot-自适应-registry--ssot)
7. [阶段五：智能体、沙箱工具与安全门禁对接 (Agent & Consent Gate)](#7-阶段五智能体沙箱工具与安全门禁对接-agent--consent-gate)
8. [阶段六：前端工作台与文档中心对接 (Frontend UX)](#8-阶段六前端工作台与文档中心对接-frontend-ux)
9. [避坑指南与安全审计要点 (Anti-Patterns & Pitfalls)](#9-避坑指南与安全审计要点-anti-patterns--pitfalls)
10. [新业务接入标准 CheckList（15分钟接入流）](#10-新业务接入标准-checklist15分钟接入流)

---

## 1. 接入背景与核心设计原则

TC Studio (原 TIM Studio) 早期仅面向腾讯云即时通信 IM（166 项自定义 REST API）。在引入腾讯云直播 (CSS) 261 项 API 3.0 之后，系统已全面演进为**腾讯云多业务即插即用型控制台与智能体协同架构**。

云直播 (CSS) 拥有 API 3.0 的全部典型特征：
- 严格遵循 `TC3-HMAC-SHA256` 签名规范；
- 存在大量复杂嵌套结构体（模板配置、转码/录制规则、复杂数组过滤）；
- 具备独立且庞大的官方错误码集与地域终端节点；
- 包含断流、禁推、删除模板等高危破坏性操作。

因此，**以云直播为标杆沉淀标准化接入指导文档**，是避免重复造轮子、保证系统健壮性与架构一致性的核心工程实践。

### 核心设计原则
1. **SSoT (Single Source of Truth, 单点真实源)**：系统提示词、前端计数、下拉菜单均不得硬编码常数，统一由注册中心动态推导。
2. **零侵入热插拔 (Plug-and-Play)**：新增业务只需置入 Catalog 与文档文件，全站（文档中心、树型导航、接口调试、Python沙箱、智能体调度）自动感知就绪。
3. **安全门禁拦截 (Consent-Gate First)**：一切高危 Action 必须通过统一安全门禁（审批弹窗），不可在沙箱或代理层出现绕过漏洞。
4. **统一抽象与特定兼容 (Unified yet Backward-Compatible)**：对外统一暴露 `cloud_call` / `cloud_search_apis`，对内保持特定业务简写（如 `live_call`、`im_call`）的向下兼容。

---

## 2. 总体架构与流水线全景图

```
                  ┌──────────────────────────────────────────────┐
                  │          腾讯云官方文档与 API 体系           │
                  └───────┬──────────────────────────────┬───────┘
                          │ 1. 目录抓取 (CGI)             │ 2. 文档与类型抓取
                          ▼                              ▼
              ┌────────────────────────┐     ┌────────────────────────┐
              │ {service}_full_catalog │     │ {service}_data_types   │
              │ .json                  │     │ .json                  │
              └───────────┬────────────┘     └───────────┬────────────┘
                          │                              │
                          └──────────────┬───────────────┘
                                         ▼
                     ┌───────────────────────────────────────┐
                     │     DynamicTimRegistry (注册中心)     │
                     │  - 自动发现所有 *_full_catalog.json   │
                     │  - SSoT 摘要 get_services_summary()   │
                     └───────────────────┬───────────────────┘
                                         │
        ┌────────────────────────────────┼────────────────────────────────┐
        ▼                                ▼                                ▼
┌─────────────────┐            ┌───────────────────┐            ┌─────────────────┐
│ Web 前端 UI      │            │ 通用调度与驱动引擎 │            │ 智能体 Agent    │
│ - Header 服务切  │            │ - UniversalClient │            │ - 动态 Prompt   │
│ - ApiTree 分类树 │            │ - Tc3Client       │            │ - CloudCallTool │
│ - 调试工作台     │            │ - TC3-HMAC-SHA256 │            │ - Python 沙箱   │
│ - 文档/错误码速查│            │ - clean_endpoint  │            │ - Consent Gate  │
└─────────────────┘            └───────────────────┘            └─────────────────┘
```

---

## 3. 阶段一：官方元数据与文档自动化同步 (Ingestion)

官方文档是元数据的权威来源。我们通过 Python 脚本直连腾讯云文档官方 CGI 自动化抓取，杜绝人工复制录入。

### 3.1 官方 CGI 与接口规范
- **CGI 地址**：`https://cloud.tencent.com/document/cgi/document/getDocPageDetail`
- **请求方法**：`POST`
- **请求头**：需伪装真实浏览器 `User-Agent`，携带 `"Accept-Encoding": "gzip, deflate"`
- **请求体格式**：
  ```json
  {
    "action": "getDocPageDetail",
    "payload": {
      "id": "20456",
      "lang": "zh",
      "isPreview": false,
      "isFromClient": true
    }
  }
  ```

### 3.2 同步流水线设计
1. **获取 API 概览页**：
   - 每个产品均有 API 概览文档（如云直播是 `doc_id: 20456`，TRTC 是 `doc_id: 44055`）；
   - 从概览页中解析表格，提取所有二级/三级分类、Action 名称、接口中文名与对应文档 `doc_id`。
2. **并发抓取详情页**：
   - 使用 `ThreadPoolExecutor`（并发数建议 8~12，搭配 `random.uniform(0.1, 0.3)` 抖动延时，防止触发 WAF 限频）；
   - 提取 Markdown 正文中的“接口描述”、“请求参数表格”、“返回参数表格”与“示例代码”。
3. **本地磁盘缓存**：
   - 缓存目录：`${TC_STUDIO_STORAGE_DIR}/docs_cache/{service}/{doc_id}.json`；
   - 存在缓存文件时跳过网络请求，支持断点续传与离线复现。

---

## 4. 阶段二：结构化数据模型与存储规范 (Schema & Storage)

### 4.1 全量接口目录：发布包 fallback 与运行期覆盖

随代码发布的 `core/{service}_full_catalog.json` 是只读 fallback。在线同步任务应将
覆盖文件写入 `${TC_STUDIO_STORAGE_DIR}/catalogs/{service}_full_catalog.json`；
注册中心优先读取运行期覆盖，缺少覆盖时才读取 `core/` 文件。这样 systemd 的
`ProtectSystem=strict` 可以继续保持，运行期同步也不会修改 `/opt/tc-studio/core`。

标准 JSON Array 格式，每个元素定义一个 API 节点：

```json
[
  {
    "api_id": "live_DescribeLiveStreamState",
    "name": "查询直播流状态",
    "action": "DescribeLiveStreamState",
    "version": "2018-08-01",
    "service_name": "云直播 (CSS)",
    "service_type": "live",
    "category": "流管理相关接口",
    "subcategory": "流状态查询",
    "command": "DescribeLiveStreamState",
    "full_command": "DescribeLiveStreamState",
    "endpoint": "live.tencentcloudapi.com",
    "doc_id": 32608,
    "doc_url": "https://cloud.tencent.com/document/api/267/32608",
    "description": "用于查询某条流是否在线，推流流速、时间等信息。",
    "fields": [
      {
        "name": "DomainName",
        "type": "string",
        "required": true,
        "description": "您的推流域名。",
        "placeholder": "123.push.tlivecloud.com",
        "help_text": "您的推流域名。"
      },
      {
        "name": "AppName",
        "type": "string",
        "required": true,
        "description": "您的推流路径，与推流和播放地址中的 AppName 保持一致，默认为 live。",
        "placeholder": "live",
        "help_text": "您的推流路径..."
      },
      {
        "name": "StreamName",
        "type": "string",
        "required": true,
        "description": "您的流名称。",
        "placeholder": "stream_test_01",
        "help_text": "您的流名称。"
      }
    ],
    "sample_payload": {
      "DomainName": "123.push.tlivecloud.com",
      "AppName": "live",
      "StreamName": "stream_test_01"
    }
  }
]
```

#### 关键字段规范要求：
- `api_id`：必须具有命名空间前缀，如 `{service}_{Action}`，防止多业务 Action 重名冲突；
- `service_type`：产品唯一标识短码（如 `live`、`trtc`、`vod`）；
- `action`：API 3.0 的真实请求 Action；
- `version`：该产品 API 3.0 的版本号（如 `2018-08-01`、`2019-07-22`）；
- `endpoint`：产品标准默认域名（如 `live.tencentcloudapi.com`、`trtc.tencentcloudapi.com`）；
- `fields`：用于前端调试工作台自动生成入参表单；
- `sample_payload`：供前端一键填入“默认调试样例”。

### 4.2 复杂结构体字典：`core/{service}_data_types.json`
API 3.0 中大量参数是结构体对象或结构体数组（如 `WatermarkInfo`）。
通过解析官方数据结构页，输出结构体字典文件：
```json
{
  "CommonMixInputParam": {
    "name": "CommonMixInputParam",
    "description": "通用混流输入参数",
    "fields": [
      { "name": "InputStreamName", "type": "string", "required": true, "description": "输入流名称" },
      { "name": "LayoutParams", "type": "CommonMixLayoutParams", "required": true, "description": "输入流布局参数" }
    ]
  }
}
```

### 4.3 公共规范与错误码：运行期缓存与发布 fallback

发布包中的 `core/{service}_common_specs.json` 和 `core/{service}_data_types.json`
只读使用。同步任务将公共规范和结构体字典写入
`${TC_STUDIO_STORAGE_DIR}/{service}_common_specs.json` 与
`${TC_STUDIO_STORAGE_DIR}/{service}_data_types.json`。

由 [`core/common_specs_syncer.py`](file:///Users/wifibaby4u/IdeaProjects/tim/core/common_specs_syncer.py) 生成，必须包含：
- `errors`：提取该业务专有错误码（如 `FailedOperation.StreamNotExist`）与 API 3.0 全局通用错误码；
- `regions`：支持该业务的地域列表（`ap-guangzhou`, `ap-beijing` 等）；
- `signature_rules`：TC3 HMAC-SHA256 规范；
- `endpoints`：地域接入域名与默认接入域名。

### 4.4 详情文档缓存存储：`${TC_STUDIO_STORAGE_DIR}/docs/{doc_id}.json`

在进入 API 文档中心时，程序优先读取运行期 docs 缓存；运行期缓存缺失时只读使用
发布包中的 `storage/docs/{doc_id}.json`，实现离线渲染。

---

## 5. 阶段三：核心通信与 TC3 签名驱动 (Signing & Dispatcher)

### 5.1 统一多业务客户端：[`UniversalCloudClient`](file:///Users/wifibaby4u/IdeaProjects/tim/core/cloud_client.py)
系统采用 `UniversalCloudClient` 作为统一调用网关，内部根据 `service` 自动分发：
- `service == "im"`：走 `TimClient`，采用 `usersig` 计算与 REST URL 路径拼接；
- `service != "im"`（如 `live`、`trtc`）：走 `TencentCloudApi3Client`，进行 TC3 签名计算。

### 5.2 严格防域名劫持规范（核心安全机制）
**⚠️ 严重避坑防范**：
在多环境配置（`config.json`）中，用户可能会针对某一业务配置专属域名（例如 `tc_endpoint: "live.tencentcloudapi.com"`）。
**绝不能**直接将该字段全局作为所有 API 3.0 业务的默认接入点，否则调用 TRTC、VOD 等业务时会发生流量劫持或报 `ResourceNotFound`！

正确的域名清洗与解析模式：
```python
def clean_endpoint(endpoint: str) -> str:
    if not endpoint:
        return ""
    ep = endpoint.strip()
    ep = re.sub(r'^https?://', '', ep)
    return ep.rstrip('/')

def resolve_endpoint(service: str, env_config: dict, default_endpoint: str = "") -> str:
    custom_endpoint = clean_endpoint(env_config.get("tc_endpoint", ""))
    
    # 仅当环境变量显式针对该业务，或显式属于该业务域名时才采用覆盖
    if custom_endpoint and (service in custom_endpoint or service == "live"):
        return custom_endpoint
        
    return default_endpoint or f"{service}.tencentcloudapi.com"
```

### 5.3 动态配置感知（Zero-Restart Config Sync）
底层的 Client 类必须使用 `@property` 动态获取配置，禁止在 `__init__` 中将配置拷贝为静态属性：
```python
class TencentCloudApi3Client:
    @property
    def config(self) -> dict:
        return config_manager.get_active_env()
```
这保证了用户在前端界面切换测试环境/生产环境或修改 SecretKey 时，所有正在运行的沙箱脚本与工具调用均立即可见最新配置，无需重启 Python 进程。

---

## 6. 阶段四：注册中心动态发现与 SSoT 自适应 (Registry & SSoT)

### 6.1 目录自动热插拔扫描机制
在 [`core/registry.py`](file:///Users/wifibaby4u/IdeaProjects/tim/core/registry.py) 中：
```python
for fn in package_catalogs_and_runtime_overrides():
    if fn.endswith("_full_catalog.json"):
        # 运行期覆盖优先，缺少覆盖时读取 package catalog
        # 自动加载并注册到 _apis 与 _ordered_apis
```
任何新增业务只要遵循 `{service}_full_catalog.json` 命名并放在发布包 `core/` 或运行期
`catalogs/` 下，注册中心就能在启动时自动注册；运行期文件变化后，其他进程在下次访问
注册中心时也会自动 reload，无需改动注册表代码。

### 6.2 SSoT 服务摘要汇总：`get_services_summary()`
注册中心对外提供唯一的统计口径方法：
```python
def get_services_summary(self) -> dict:
    self.discover()
    total_apis = len(self._ordered_apis)
    services = self.list_services()
    ...
    return {
        "total_apis": total_apis,
        "services_count": len(services),
        "service_names": service_names,
        "breakdown_text": breakdown_text,
        "prompt_text": prompt_text
    }
```
所有下游消费者（提示词生成器、前端 API 状态接口、诊断工具）均基于此数据源渲染。

---

## 7. 阶段五：智能体、沙箱工具与安全门禁对接 (Agent & Consent Gate)

### 7.1 统一工具集暴露规范
在 [`core/agent/tools.py`](file:///Users/wifibaby4u/IdeaProjects/tim/core/agent/tools.py) 中，向大模型暴露 3 项核心云能力工具：
1. `cloud_call(action_or_path, payload, service=None)`：全能多业务调用工具；
2. `cloud_search_apis(keyword, service="all")`：跨产品语义与 Action 检索；
3. `cloud_get_doc(action_or_path)`：查看入参出参定义与结构体说明。

### 7.2 安全审批门禁与高危拦截 (Consent Gate)
#### 7.2.1 高危操作定义表
在 [`core/agent/tools.py`](file:///Users/wifibaby4u/IdeaProjects/tim/core/agent/tools.py#L42-L65) 的 `HIGH_RISK_ACTIONS` 中登记高危操作：
- **云直播 (CSS)**：`DropLiveStream`（强制断流）、`ForbidLiveStream`（禁推封禁）、`DeleteLiveWatermark` 等；
- **即时通信 (IM)**：`forbid_send_msg`（全员禁言）、`destroy_group`（解散群组）、`delete_account` 等；
- **新业务 (如 TRTC/VOD)**：必须将销毁房间、删除媒体、清空配置等 Action 加入此字典。

#### 7.2.2 门禁拦截与字符串入参解析
LLM 生成调用参数时，偶尔会将 JSON 序列化为字符串传递。工具内部必须具备健壮的反序列化机制，严防因类型不符导致安全门禁漏判：
```python
parsed_payload = payload
if isinstance(payload, str):
    try:
        parsed_payload = json.loads(payload)
    except Exception:
        parsed_payload = ast.literal_eval(payload)

# 提取关键目标概要（如 StreamName / RoomId）向用户弹窗确认
target = parsed_payload.get("StreamName") or parsed_payload.get("GroupId")
if self.approval_requester and not self.approval_requester(action, target, parsed_payload):
    return {"ActionStatus": "CANCELLED", "ErrorInfo": "用户拒绝了执行高危操作"}
```

#### 7.2.3 Python 沙箱双重保障
在 [`core/agent/engine.py`](file:///Users/wifibaby4u/IdeaProjects/tim/core/agent/engine.py) 中，沙箱内置的快捷方法：
- `live_call(action, payload)`
- `trtc_call(action, payload)`
- `im_call(path, payload)`

**必须统一经过 `CloudCallTool.forward()` 转发**，绝不允许直接挂接底层的 `client.invoke()`，防止 Python 脚本绕过安全审批机制。

---

## 8. 阶段六：前端工作台与文档中心对接 (Frontend UX)

### 8.1 全局服务切换上下文：[`apiStore.ts`](file:///Users/wifibaby4u/IdeaProjects/tim/web/src/stores/apiStore.ts)
```typescript
export interface CloudService {
  id: string
  name: string
  shortName: string
  tag: string
  description: string
  iconName: 'message' | 'video' | 'cloud'
  badgeColor: string
}

export const CLOUD_SERVICES: CloudService[] = [
  { id: 'im', name: '即时通信 IM', tag: 'TIM', ... },
  { id: 'live', name: '云直播 (CSS)', tag: 'CSS', ... },
  // 新增业务直接在此处增加一条元数据即可
]
```

### 8.2 接口调试工作台 ([`ApiWorkbenchPanel.tsx`](file:///Users/wifibaby4u/IdeaProjects/tim/web/src/components/panels/ApiWorkbenchPanel.tsx))
- **左侧分类树**：通过 `api.service_type === activeServiceId` 自动隔离显示当前产品的接口目录；
- **参数构建表单**：自动读取 `api.fields`，根据 `type` 渲染为 Input（字符串）、NumberInput（整型/浮点型）、Switch（布尔）或 JSON CodeEditor（复杂对象/数组）；
- **动态签名预览**：针对 API 3.0 接口，自动在面板中实时计算并展示 `Authorization: TC3-HMAC-SHA256 ...`，方便开发者复制到 Postman 或 cURL。

### 8.3 官方规范中心 ([`CommonSpecsViewer.tsx`](file:///Users/wifibaby4u/IdeaProjects/tim/web/src/components/panels/CommonSpecsViewer.tsx))
- 支持按业务切换查阅官方规范；
- 错误码面板提供输入过滤，自动高亮业务错误码（如 `ResourceNotFound`）与排查建议；
- 地域列表支持复制地域代号（如 `ap-guangzhou`）。

---

## 9. 避坑指南与安全审计要点 (Anti-Patterns & Pitfalls)

| 序号 | 典型错误 (Anti-Pattern) | 危害与现象 | 正确做法 (Best Practice) |
|---|---|---|---|
| 1 | **提示词硬编码数字** | 增加新业务后提示词陈旧不符，LLM 认知混乱 | 由 `registry.get_services_summary()` 运行时注入提示词 |
| 2 | **全局共享 tc_endpoint** | 调云直播时配置了域名，切到 TRTC 后全部被污染报错 | 用 `resolve_endpoint()` 动态按 `service` 路由，仅在明确匹配时覆盖 |
| 3 | **沙箱工具绕过审批** | 用户在对话中受拦截，但在 Python 沙箱中执行脚本可直接删除资源 | 沙箱内所有快捷调用（`live_call` 等）统一收口走 `CloudCallTool.forward()` |
| 4 | **Client 缓存静态 Config** | 切换测试/生产环境后，调用依然请求旧环境 | Client 类中使用 `@property def config` 实时向 `config_manager` 取值 |
| 5 | **忽略字符串 Payload** | 大模型传参为字符串时 `isinstance(dict)` 为假，丢失审批目标 | 在工具层统一前置 `json.loads` / `ast.literal_eval` 反序列化 |
| 6 | **缺少服务命名空间** | 不同云产品的 Action 重名（如 `CreateUser`）导致相互覆盖覆盖 | `api_id` 强制采用 `{service}_{Action}` 命名空间命名 |

---

## 10. 新业务接入标准 CheckList（15分钟接入流）

当团队计划接入下一个腾讯云 API 3.0 业务（例如 **TRTC 实时音视频**）时，执行以下标准流程：

```bash
# 步骤 1：确认官方概览文档 ID 并执行拉取
python3 scripts/sync_trtc_catalog.py
# -> 发布构建产出 core/trtc_full_catalog.json；线上同步覆盖写入
#    ${TC_STUDIO_STORAGE_DIR}/catalogs/trtc_full_catalog.json

# 步骤 2：抓取官方文档与结构体字典
python3 scripts/audit_and_sync_trtc_docs.py
# -> 产出 ${TC_STUDIO_STORAGE_DIR}/trtc_data_types.json 与
#    ${TC_STUDIO_STORAGE_DIR}/docs/xxx.json

# 步骤 3：同步错误码与公共规范
python3 scripts/sync_trtc_common_specs.py
# -> 产出 ${TC_STUDIO_STORAGE_DIR}/trtc_common_specs.json

# 步骤 4：在高危拦截表中登记敏感 Action
# 编辑 core/agent/tools.py 中的 HIGH_RISK_ACTIONS，加入 trtc 高危行为

# 步骤 5：在前端增加服务元数据
# 编辑 web/src/stores/apiStore.ts 中的 CLOUD_SERVICES 添加 trtc

# 步骤 6：运行验证测试
rtk pytest
cd web && pnpm build
```

✅ **完成以上步骤后，系统将自动达成**：
1. 顶部 Header 出现新业务切换菜单；
2. API 树、调试工作台、参数表单、TC3 签名预览全部就绪；
3. API 文档中心与错误码诊断自动生效；
4. 智能体系统提示词自动同步最新接口总数与业务介绍；
5. 智能体可通过自然语言或沙箱脚本调度新业务接口，高危操作自动触发安全审批！
