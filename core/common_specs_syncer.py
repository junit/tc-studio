# -*- coding: utf-8 -*-
"""
腾讯云 API 3.0 公共文档与标准规范同步引擎
包含：
- 20458: 请求结构 (Request Structure)
- 20459: 公共参数 (Common Parameters)
- 30661: 签名方法 v3 (TC3-HMAC-SHA256)
- 20460: 签名方法 v1 (Legacy HMAC-SHA1/SHA256)
- 20461: 返回结果 (Response Structure)
- 78521: 参数类型 (Supported Parameter Types)
- 20474: 数据结构 (Data Structures - 138 Structs)
- 30427: 错误码 (Error Codes - Global & Business)
- 34019: 映射表 (Appendices - Country/Province/ISP)
"""

import html
import json
import os
import re
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any

from core.file_storage import atomic_write_json
from core.runtime_paths import (
    package_data_types_path,
    package_specs_path,
    runtime_data_types_path,
    runtime_docs_dir,
    runtime_specs_path,
)
from core.tree_syncer import fetch_doc_detail_api

STORAGE_DIR = str(runtime_docs_dir())
SPECS_CACHE_PATH = str(runtime_specs_path("live"))
DATA_TYPES_PATH = str(runtime_data_types_path("live"))
BEIJING_TZ = timezone(timedelta(hours=8))


def _specs_cache_path(service: str) -> str:
    if str(service or "live").lower() == "live":
        return SPECS_CACHE_PATH
    return str(runtime_specs_path(service))


def _cache_signature_for_path(path: str) -> tuple[int, int] | None:
    try:
        stat = os.stat(path)
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


def _load_cached_specs(service: str) -> dict | None:
    """Load a valid runtime cache or fall back to the package snapshot."""

    candidates = (
        _specs_cache_path(service),
        str(package_specs_path(service)),
    )
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen or not os.path.exists(candidate):
            continue
        seen.add(candidate)
        try:
            with open(candidate, "r", encoding="utf-8") as stream:
                loaded = json.load(stream)
            if isinstance(loaded, dict):
                return loaded
            print(f"[WARN] Ignoring invalid common specs cache {candidate}: root is not an object")
        except (OSError, ValueError) as exc:
            print(f"[WARN] Failed to load common specs cache {candidate}: {exc}")
    return None


def _data_types_read_path() -> str:
    for candidate in (DATA_TYPES_PATH, str(package_data_types_path("live"))):
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as stream:
                loaded = json.load(stream)
            if isinstance(loaded, dict):
                return candidate
        except (OSError, ValueError):
            continue
    return DATA_TYPES_PATH

COMMON_DOC_CONFIGS = [
    {"id": 20458, "title": "请求结构", "category": "invocation", "key": "request_structure", "url": "https://cloud.tencent.com/document/api/267/20458"},
    {"id": 20459, "title": "公共参数", "category": "invocation", "key": "common_params", "url": "https://cloud.tencent.com/document/api/267/20459"},
    {"id": 30661, "title": "签名方法 v3", "category": "invocation", "key": "signature_v3", "url": "https://cloud.tencent.com/document/api/267/30661"},
    {"id": 20460, "title": "签名方法 v1", "category": "invocation", "key": "signature_v1", "url": "https://cloud.tencent.com/document/api/267/20460"},
    {"id": 20461, "title": "返回结果", "category": "invocation", "key": "response_structure", "url": "https://cloud.tencent.com/document/api/267/20461"},
    {"id": 78521, "title": "参数类型", "category": "invocation", "key": "param_types", "url": "https://cloud.tencent.com/document/api/267/78521"},
    {"id": 20474, "title": "数据结构", "category": "structs", "key": "data_structures", "url": "https://cloud.tencent.com/document/api/267/20474"},
    {"id": 30427, "title": "错误码", "category": "errors", "key": "error_codes", "url": "https://cloud.tencent.com/document/api/267/30427"},
    {"id": 34019, "title": "映射表", "category": "mappings", "key": "appendices", "url": "https://cloud.tencent.com/document/api/267/34019"},
]

# 常见错误解决方案字典（辅助官方简短描述）
ERROR_SOLUTIONS = {
    "AuthFailure": "请检查 SecretId、SecretKey 是否配置正确，且具备当前业务 API 的 CAM 权限。",
    "AuthFailure.InvalidAuthorization": "请求头中的 Authorization 格式不正确。请确认签名算法为 TC3-HMAC-SHA256 并包含完整 Credential、SignedHeaders 与 Signature。",
    "AuthFailure.InvalidSecretId": "SecretId 不存在或非法。请在腾讯云控制台【访问管理】中核对密钥 ID。",
    "AuthFailure.SecretIdNotFound": "SecretId 未找到，请核对是否已被停用或删除。",
    "AuthFailure.SignatureExpire": "请求时间戳 X-TC-Timestamp 与腾讯云服务器时间差超过5分钟，请校准本地系统时钟。",
    "AuthFailure.SignatureFailure": "签名计算不匹配。请确认请求 Body、HTTP Header 排序以及 SecretKey 计算过程与官方 TC3 签名规范一致。",
    "AuthFailure.TokenFailure": "安全凭证 Token 校验失败或已过期。如果使用临时密钥，请核对 SessionToken。",
    "FailedOperation.StreamNotExist": "指定直播流不存在或未在推流中。请确认 StreamName 是否拼写正确或该流处于活跃推流状态。",
    "FailedOperation.AiTranscodeOptionFail": "操作极速高清/AI转码失败，请确认控制台是否已开通该高级转码特性。",
    "FailedOperation.AlterStreamFailed": "修改流状态失败，请确认当前流是否存在且未处于冲突状态。",
    "FailedOperation.TaskNotExist": "指定任务不存在或已过期，请核对 TaskId 是否准确。",
    "InvalidParameter.DomainNotFound": "指定的域名不存在或未在云直播控制台添加配置。",
    "InvalidParameter.DomainInvalid": "域名状态异常或未通过工信部 ICP 备案/腾讯云审核。",
    "InvalidParameter.DomainAlreadyExist": "该域名已存在，不能重复添加。",
    "InvalidParameter.StreamNameInvalid": "流名称非法，流名仅支持字母、数字、下划线、短横线，长度不能超过256字符。",
    "InvalidParameter.TimeInvalid": "时间范围参数非法，请核对 StartTime / EndTime 格式 (如 UTC 或 RFC3339 格式) 且开始时间必须早于结束时间。",
    "InvalidParameter.TemplateNotExist": "指定的转码/水印/录制模板不存在，请在控制台确认模板 ID。",
    "LimitExceeded.RuleNumLimit": "规则数量已达到上限，请删除不需要的规则后再创建。",
    "RequestLimitExceeded": "超出接口调用频次限制 (QPS 配额)。建议降低并发频率或采用指数退避重试。",
    "ResourceNotFound": "请求的资源不存在，请检查参数中传递的资源 ID 是否正确。",
    "ResourceUnavailable": "资源当前处于不可用状态或已被冻结。",
    "UnauthorizedOperation": "当前 CAM 账号缺少操作该接口的权限策略，请联系管理员授予权限。",
}


def _strip_html(raw: str) -> str:
    """去除 HTML 标签并反转义"""
    if not raw:
        return ""
    clean = re.sub(r"<[^>]+>", " ", raw)
    return html.unescape(" ".join(clean.split()))


def _save_raw_doc(doc_id: int, detail: dict, title: str, doc_url: str):
    """将获取的原始文档规范保存到运行期 docs 缓存。"""
    os.makedirs(STORAGE_DIR, exist_ok=True)
    filepath = os.path.join(STORAGE_DIR, f"{doc_id}.json")
    record = {
        "doc_id": doc_id,
        "api_id": f"common_{doc_id}",
        "title": detail.get("title") or title,
        "doc_url": doc_url,
        "sync_time": datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "status": "synced",
        "source_mode": "api",
        "requested_mode": "api",
        "overview": _strip_html(detail.get("description", detail.get("body", "")[:300])),
        "raw_body": detail.get("body", ""),
        "rate_limit": "20 QPS (API 3.0 默认)",
        "url_params": [],
        "request_body_fields": [],
        "request_examples": [],
        "response_body_fields": [],
        "response_examples": [],
        "error_codes": [],
    }
    atomic_write_json(filepath, record, mode=0o600)


def parse_request_structure_20458(body: str, title: str) -> dict:
    tables = re.findall(r"<table[^>]*>(.*?)</table>", body, re.DOTALL)
    endpoints = []
    if len(tables) > 0:
        for r in re.findall(r"<tr[^>]*>(.*?)</tr>", tables[0], re.DOTALL)[1:]:
            cells = [_strip_html(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.DOTALL)]
            if len(cells) >= 2:
                endpoints.append({"region_name": cells[0], "endpoint": cells[1]})
    financial_endpoints = []
    if len(tables) > 1:
        for r in re.findall(r"<tr[^>]*>(.*?)</tr>", tables[1], re.DOTALL)[1:]:
            cells = [_strip_html(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.DOTALL)]
            if len(cells) >= 2:
                financial_endpoints.append({"region_name": cells[0], "endpoint": cells[1]})

    return {
        "doc_id": 20458,
        "title": title or "请求结构",
        "doc_url": "https://cloud.tencent.com/document/api/267/20458",
        "protocols": ["HTTPS", "HTTP"],
        "methods": ["POST", "GET"],
        "default_method": "POST",
        "content_types": ["application/json", "application/x-www-form-urlencoded"],
        "default_content_type": "application/json",
        "charset": "UTF-8",
        "default_endpoint": "live.tencentcloudapi.com",
        "endpoints": endpoints,
        "financial_endpoints": financial_endpoints,
    }


def parse_common_params_20459(body: str, title: str) -> dict:
    tables = re.findall(r"<table[^>]*>(.*?)</table>", body, re.DOTALL)
    v3_headers = []
    if len(tables) > 0:
        for r in re.findall(r"<tr[^>]*>(.*?)</tr>", tables[0], re.DOTALL)[1:]:
            cells = [_strip_html(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.DOTALL)]
            if len(cells) >= 4:
                m = re.search(r"HTTP 请求头[：:]\s*([X\-A-Za-z0-9]+)", cells[3])
                header_name = m.group(1) if m else cells[0]
                v3_headers.append({
                    "param": cells[0],
                    "header": header_name,
                    "type": cells[1],
                    "required": cells[2] == "是",
                    "desc": cells[3],
                })
    v1_params = []
    if len(tables) > 1:
        for r in re.findall(r"<tr[^>]*>(.*?)</tr>", tables[1], re.DOTALL)[1:]:
            cells = [_strip_html(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.DOTALL)]
            if len(cells) >= 4:
                v1_params.append({
                    "param": cells[0],
                    "type": cells[1],
                    "required": cells[2] == "是",
                    "desc": cells[3],
                })
    regions = []
    if len(tables) > 2:
        for r in re.findall(r"<tr[^>]*>(.*?)</tr>", tables[2], re.DOTALL)[1:]:
            cells = [_strip_html(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.DOTALL)]
            if len(cells) >= 2:
                regions.append({"region_name": cells[0], "region_code": cells[1]})

    return {
        "doc_id": 20459,
        "title": title or "公共参数",
        "doc_url": "https://cloud.tencent.com/document/api/267/20459",
        "v3_headers": v3_headers,
        "v1_params": v1_params,
        "regions": regions,
    }


def parse_signature_v3_30661(body: str, title: str) -> dict:
    return {
        "doc_id": 30661,
        "title": title or "签名方法 v3",
        "doc_url": "https://cloud.tencent.com/document/api/267/30661",
        "algorithm": "TC3-HMAC-SHA256",
        "header_name": "Authorization",
        "steps": [
            {
                "step": 1,
                "name": "拼接规范请求串 (CanonicalRequest)",
                "formula": "HTTPRequestMethod + '\\n' + CanonicalURI + '\\n' + CanonicalQueryString + '\\n' + CanonicalHeaders + '\\n' + SignedHeaders + '\\n' + HashedRequestPayload",
            },
            {
                "step": 2,
                "name": "拼接待签名字符串 (StringToSign)",
                "formula": "'TC3-HMAC-SHA256' + '\\n' + RequestTimestamp + '\\n' + CredentialScope + '\\n' + HashedCanonicalRequest",
            },
            {
                "step": 3,
                "name": "计算派生密钥与签名 (Signature)",
                "formula": "HMAC-SHA256(SecretSigning, StringToSign)",
            },
            {
                "step": 4,
                "name": "拼接 Authorization 请求头",
                "formula": "TC3-HMAC-SHA256 Credential=SecretId/Date/Service/tc3_request, SignedHeaders=SignedHeaders, Signature=Signature",
            },
        ],
        "auth_header_format": "TC3-HMAC-SHA256 Credential=SecretId/Date/Service/tc3_request, SignedHeaders=SignedHeaders, Signature=Signature",
    }


def parse_signature_v1_20460(body: str, title: str) -> dict:
    return {
        "doc_id": 20460,
        "title": title or "签名方法 v1",
        "doc_url": "https://cloud.tencent.com/document/api/267/20460",
        "algorithm": "HmacSHA1 / HmacSHA256",
        "status": "deprecated",
        "description": "旧版 v1 签名算法（已不推荐，新业务统一使用 v3 签名）。",
    }


def parse_response_structure_20461(body: str, title: str) -> dict:
    return {
        "doc_id": 20461,
        "title": title or "返回结果",
        "doc_url": "https://cloud.tencent.com/document/api/267/20461",
        "envelope_key": "Response",
        "success_envelope": {
            "Response": {
                "RequestId": "b5b41468-520d-4192-b42f-595cc34b6c1c",
                "TotalCount": 0,
                "<业务数据字段>": "...",
            }
        },
        "error_envelope": {
            "Response": {
                "Error": {
                    "Code": "AuthFailure.SignatureFailure",
                    "Message": "The provided credentials could not be validated. Please check your signature is correct.",
                },
                "RequestId": "ed4501c6-0867-4fc0-a3fe-ced510dc1cb7",
            }
        },
    }


def parse_param_types_78521(body: str, title: str) -> dict:
    items = re.findall(r"<li>(.*?)</li>", body, re.DOTALL)
    types_list = []
    for it in items:
        clean = _strip_html(it)
        if "：" in clean or ":" in clean:
            sep = "：" if "：" in clean else ":"
            t_name, t_desc = clean.split(sep, 1)
            types_list.append({"type": t_name.strip(), "description": t_desc.strip()})
        elif clean:
            types_list.append({"type": clean, "description": ""})

    return {
        "doc_id": 78521,
        "title": title or "参数类型",
        "doc_url": "https://cloud.tencent.com/document/api/267/78521",
        "types": types_list,
    }


def parse_data_structures_20474(body: str, title: str) -> dict:
    """解析 20474 数据结构中的 138 个结构体"""
    pattern = re.compile(
        r"<h[2-4][^>]*>(.*?)</h[2-4]>(.*?)(?=(?:<h[2-4]|\Z))",
        re.DOTALL,
    )
    structs = {}
    for m in pattern.finditer(body):
        raw_name = _strip_html(m.group(1))
        content = m.group(2)
        if not re.match(r"^[A-Z][a-zA-Z0-9_]+$", raw_name):
            continue

        table_m = re.search(r"<table[^>]*>(.*?)</table>", content, re.DOTALL)
        if not table_m:
            continue

        fields = []
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_m.group(1), re.DOTALL)
        for r in rows[1:]:
            cells = [_strip_html(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.DOTALL)]
            if len(cells) >= 4:
                fields.append({
                    "name": cells[0],
                    "type": cells[1],
                    "required": cells[2] == "是",
                    "description": cells[3],
                })
        if fields:
            structs[raw_name] = fields

    # 如果存在本地已缓存的 live_data_types.json 且当前解析数量少，融合增强
    cached_data_types_path = _data_types_read_path()
    if os.path.exists(cached_data_types_path):
        try:
            with open(cached_data_types_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
                for k, v in cached.items():
                    if k not in structs:
                        structs[k] = v
        except Exception:
            pass

    return {
        "doc_id": 20474,
        "title": title or "数据结构",
        "doc_url": "https://cloud.tencent.com/document/api/267/20474",
        "total_count": len(structs),
        "structs": structs,
    }


def parse_error_codes_30427(body: str, title: str) -> dict:
    tables = re.findall(r"<table[^>]*>(.*?)</table>", body, re.DOTALL)
    global_errors = []
    business_errors = []
    error_map = {}

    if len(tables) > 0:
        for r in re.findall(r"<tr[^>]*>(.*?)</tr>", tables[0], re.DOTALL)[1:]:
            cells = [_strip_html(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.DOTALL)]
            if len(cells) >= 2:
                code, desc = cells[0], cells[1]
                solution = ERROR_SOLUTIONS.get(code) or "请按照官方错误描述检查请求参数或账户状态。"
                item = {
                    "code": code,
                    "desc": desc,
                    "solution": solution,
                    "scope": "global",
                }
                global_errors.append(item)
                error_map[code] = item

    if len(tables) > 1:
        for r in re.findall(r"<tr[^>]*>(.*?)</tr>", tables[1], re.DOTALL)[1:]:
            cells = [_strip_html(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.DOTALL)]
            if len(cells) >= 2:
                code, desc = cells[0], cells[1]
                solution = ERROR_SOLUTIONS.get(code) or "请核对直播流名、域名配置、模板或任务参数是否正常。"
                item = {
                    "code": code,
                    "desc": desc,
                    "solution": solution,
                    "scope": "business",
                }
                business_errors.append(item)
                error_map[code] = item

    return {
        "doc_id": 30427,
        "title": title or "错误码",
        "doc_url": "https://cloud.tencent.com/document/api/267/30427",
        "total_count": len(error_map),
        "global_count": len(global_errors),
        "business_count": len(business_errors),
        "global_errors": global_errors,
        "business_errors": business_errors,
        "error_map": error_map,
    }


def parse_mappings_34019(body: str, title: str) -> dict:
    tables = re.findall(r"<table[^>]*>(.*?)</table>", body, re.DOTALL)
    countries = []
    provinces = []
    isps = []

    if len(tables) > 0:
        for r in re.findall(r"<tr[^>]*>(.*?)</tr>", tables[0], re.DOTALL)[1:]:
            cells = [_strip_html(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.DOTALL)]
            if len(cells) >= 2:
                countries.append({"zh": cells[0], "en": cells[1]})
    if len(tables) > 1:
        for r in re.findall(r"<tr[^>]*>(.*?)</tr>", tables[1], re.DOTALL)[1:]:
            cells = [_strip_html(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.DOTALL)]
            if len(cells) >= 2:
                provinces.append({"zh": cells[0], "en": cells[1]})
    if len(tables) > 2:
        for r in re.findall(r"<tr[^>]*>(.*?)</tr>", tables[2], re.DOTALL)[1:]:
            cells = [_strip_html(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.DOTALL)]
            if len(cells) >= 2:
                isps.append({"zh": cells[0], "en": cells[1]})

    return {
        "doc_id": 34019,
        "title": title or "映射表",
        "doc_url": "https://cloud.tencent.com/document/api/267/34019",
        "countries_count": len(countries),
        "provinces_count": len(provinces),
        "isps_count": len(isps),
        "countries": countries,
        "provinces": provinces,
        "isps": isps,
    }


class CommonSpecsManager:
    """腾讯云公共文档与 3.0 规范管理器"""

    def __init__(self):
        self._lock = threading.Lock()
        # Network synchronization must not hold the cache/read lock.  Keep a
        # separate single-flight lock for concurrent explicit sync requests.
        self._sync_lock = threading.Lock()
        self._specs_cache: dict[str, dict] = {}
        self._specs_cache_signature: dict[str, tuple[int, int] | None] = {}

    def get_specs(self, service: str = "live", force_reload: bool = False) -> dict:
        """获取结构化公共规范；不在持锁状态下进入同步 I/O。"""
        cache_path = _specs_cache_path(service)
        try:
            stat = os.stat(cache_path)
            cache_signature: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            cache_signature = None
        with self._lock:
            if (
                not force_reload
                and service in self._specs_cache
                and self._specs_cache_signature.get(service) == cache_signature
            ):
                return self._specs_cache[service]

            if not force_reload:
                loaded = _load_cached_specs(service)
                if loaded is not None:
                    loaded_service = str(loaded.get("service") or "live")
                    self._specs_cache[loaded_service] = loaded
                    self._specs_cache_signature[loaded_service] = cache_signature
                    if loaded_service == service:
                        return loaded

        # sync_all acquires the lock itself; never call it from the lock above.
        return self.sync_all(service=service)

    def sync_all(self, service: str = "live") -> dict:
        """全量同步 9 篇公共规范文档并结构化存储"""
        with self._sync_lock:
            print(f"[INFO] Starting Tencent Cloud API 3.0 Common Specs Sync for service={service}...")
            cache_path = _specs_cache_path(service)
            previous_specs = _load_cached_specs(service)
            specs: Dict[str, Any] = {
                "service": service,
                "service_name": "云直播 (CSS)" if service == "live" else f"腾讯云 {service.upper()}",
                "updated_at": datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                "total_docs": len(COMMON_DOC_CONFIGS),
                "invocation": {},
                "structs": {},
                "errors": {},
                "mappings": {},
            }
            if isinstance(previous_specs, dict):
                for category in ("invocation", "structs", "errors", "mappings"):
                    previous_value = previous_specs.get(category)
                    if isinstance(previous_value, dict):
                        specs[category] = previous_value
            synced_doc_count = 0

            for cfg in COMMON_DOC_CONFIGS:
                doc_id = cfg["id"]
                title = cfg["title"]
                url = cfg["url"]
                key = cfg["key"]
                cat = cfg["category"]

                try:
                    detail = fetch_doc_detail_api(doc_id)
                    if not detail:
                        print(f"[WARN] Failed to fetch doc detail for {doc_id} ({title})")
                        continue
                    synced_doc_count += 1

                    # 保存原始格式至运行期 docs 缓存
                    _save_raw_doc(doc_id, detail, title, url)
                    body = detail.get("body", "")

                    if doc_id == 20458:
                        specs["invocation"]["request_structure"] = parse_request_structure_20458(body, title)
                    elif doc_id == 20459:
                        specs["invocation"]["common_params"] = parse_common_params_20459(body, title)
                    elif doc_id == 30661:
                        specs["invocation"]["signature_v3"] = parse_signature_v3_30661(body, title)
                    elif doc_id == 20460:
                        specs["invocation"]["signature_v1"] = parse_signature_v1_20460(body, title)
                    elif doc_id == 20461:
                        specs["invocation"]["response_structure"] = parse_response_structure_20461(body, title)
                    elif doc_id == 78521:
                        specs["invocation"]["param_types"] = parse_param_types_78521(body, title)
                    elif doc_id == 20474:
                        parsed_structs = parse_data_structures_20474(body, title)
                        specs["structs"] = parsed_structs
                        # 同时也确保运行期 data types 缓存得到持久化更新
                        if parsed_structs.get("structs"):
                            atomic_write_json(
                                DATA_TYPES_PATH,
                                parsed_structs["structs"],
                                mode=0o644,
                            )
                    elif doc_id == 30427:
                        specs["errors"] = parse_error_codes_30427(body, title)
                    elif doc_id == 34019:
                        specs["mappings"] = parse_mappings_34019(body, title)

                    print(f"[OK] Synced and parsed common doc {doc_id}: {title}")
                except Exception as e:
                    print(f"[ERROR] Failed syncing common doc {doc_id} ({title}): {e}")

            if synced_doc_count == 0:
                if isinstance(previous_specs, dict):
                    print(
                        "[WARN] Common specs sync returned no usable documents; "
                        "keeping the last known-good cache"
                    )
                    with self._lock:
                        self._specs_cache[service] = previous_specs
                        self._specs_cache_signature[service] = _cache_signature_for_path(
                            cache_path
                        )
                    return previous_specs
                raise RuntimeError("公共规范同步未获取到任何有效文档，拒绝发布空缓存")

            # 保存解析后结构化数据至运行期 common specs 缓存
            atomic_write_json(cache_path, specs, mode=0o644)

            with self._lock:
                self._specs_cache[service] = specs
                self._specs_cache_signature[service] = _cache_signature_for_path(cache_path)
            print(f"[SUCCESS] Tencent Cloud API 3.0 Common Specs successfully synced to {cache_path}!")
            return specs

    def lookup_error(self, code: str) -> Optional[dict]:
        """按错误码检索官方详细说明与解决建议"""
        specs = self.get_specs()
        err_map = specs.get("errors", {}).get("error_map", {})
        if code in err_map:
            return err_map[code]

        # 尝试匹配前缀 (例如 FailedOperation.*)
        for k, v in err_map.items():
            if code.startswith(k) or k.startswith(code):
                return v
        return None

    def get_struct(self, struct_name: str) -> Optional[List[dict]]:
        """按结构体名称获取字段定义"""
        specs = self.get_specs()
        structs = specs.get("structs", {}).get("structs", {})
        return structs.get(struct_name)


# 全局单例
common_specs_manager = CommonSpecsManager()
