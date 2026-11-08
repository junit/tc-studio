# -*- coding: utf-8 -*-
"""
腾讯云 IM 官方文档结构化解析与异步/批量同步引擎
"""

import concurrent.futures
from datetime import datetime, timezone, timedelta
import gzip
import html
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Optional

from core.file_storage import atomic_write_json
from core.sync_task_manager import SyncTaskCancelled, SyncTaskManager

# 存储目录
STORAGE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "storage", "docs"))
os.makedirs(STORAGE_DIR, mode=0o700, exist_ok=True)
try:
    os.chmod(STORAGE_DIR, 0o700)
except OSError:
    pass

BEIJING_TZ = timezone(timedelta(hours=8))


def get_current_time_str() -> str:
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def get_doc_path(doc_id: int) -> str:
    return os.path.join(STORAGE_DIR, f"{doc_id}.json")


def clean_json_str(raw: str) -> str:
    """尝试将带注释或特殊字符的 JSON 文本清洗为格式化标准 JSON，失败则保留原字符串"""
    if not raw:
        return ""
    # 1. 尝试直接标准 JSON 解析
    try:
        parsed = json.loads(raw.strip(), strict=False)
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    except Exception:
        pass

    # 2. 若包含 HTTP 头或包装文本，截取第一个 { 或 [ 到最后一个 } 或 ] 之间的内容
    s_obj = raw.find("{")
    s_arr = raw.find("[")
    s_idx = -1
    if s_obj != -1 and s_arr != -1:
        s_idx = min(s_obj, s_arr)
    elif s_obj != -1:
        s_idx = s_obj
    elif s_arr != -1:
        s_idx = s_arr

    e_idx = max(raw.rfind("}"), raw.rfind("]"))
    target = raw[s_idx:e_idx + 1] if s_idx != -1 and e_idx > s_idx else raw

    try:
        parsed = json.loads(target, strict=False)
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    except Exception:
        pass

    # 3. 容错清洗：移除单行与多行注释（排除 http:// 与 https:// 中的 //），移除末尾逗号和省略号
    no_comments = re.sub(r"(?<!:)\s*//[^\n]*", "", target)
    no_comments = re.sub(r"/\*.*?\*/", "", no_comments, flags=re.DOTALL)
    no_trailing = re.sub(r",\s*([\}\]])", r"\1", no_comments)
    no_dots = re.sub(r"\.\.\.+", "", no_trailing)
    try:
        parsed = json.loads(no_dots, strict=False)
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    except Exception:
        return raw.strip()


def parse_url_to_body(raw_text: str) -> dict:
    """从 HTTP GET / 拼接参数 URL 中提取业务入参字典（用于腾讯云 API 3.0 输入示例解析）"""
    text = raw_text.replace("\n&", "&").replace("\n", "").strip()
    if "?" in text:
        qs = text.split("?", 1)[1]
    else:
        qs = text
    # 剥除占位标记如 <公共请求参数>
    qs = re.sub(r"&?<[^>]+>&?", "&", qs).strip("&")
    parsed = urllib.parse.parse_qs(qs, keep_blank_values=True)
    body = {}
    common_params = {
        "Action", "Version", "Region", "Timestamp", "Nonce", "Signature",
        "SignatureMethod", "Token", "<公共请求参数>"
    }
    for k, v in parsed.items():
        if k in common_params:
            continue
        val = v[0] if len(v) == 1 else v
        value_text = str(val)
        if value_text.isdigit():
            val = int(value_text)
        elif value_text.lower() == "true":
            val = True
        elif str(val).lower() == "false":
            val = False

        if "." in k:
            base_k, idx = k.split(".", 1)
            if idx == "N" or idx.isdigit():
                if base_k not in body:
                    body[base_k] = []
                if isinstance(val, str) and (val.startswith("[") or val.startswith('"')):
                    try:
                        inner = json.loads(val)
                        if isinstance(inner, list):
                            body[base_k].extend(inner)
                        else:
                            body[base_k].append(inner)
                        continue
                    except Exception:
                        pass
                body[base_k].append(val)
                continue
        body[k] = val
    return body


def parse_official_doc(
    doc_id: int,
    api_id: str = "",
    doc_url: str = "",
    mode: str = "auto",  # 'auto' | 'html' | 'api'
) -> dict:
    """
    抓取腾讯云 IM 官方文档并结构化解析为标准数据模型：
    - mode='auto': 优先通过官方 getDocPageDetail 接口拉取结构化正文，若失败或受限优雅回退至原生网页 HTML 抓取；
    - mode='html': 纯净抓取官方原生网页 HTML 解析（防封防限频，不调用动态 CGI 接口）；
    - mode='api': 纯净调用官方 getDocPageDetail 接口（极小数据量快速提取）。
    """
    if not doc_url:
        doc_url = f"https://cloud.tencent.com/document/product/269/{doc_id}"

    text = ""
    title = ""
    overview = ""
    actual_source = "html"

    # 1. 若为 auto 或 api 模式，优先尝试官方 getDocPageDetail 接口
    detail = None
    if mode in ("auto", "api"):
        try:
            from core.tree_syncer import fetch_doc_detail_api
            detail = fetch_doc_detail_api(doc_id)
            if detail and detail.get("body"):
                text = detail.get("body", "")
                title = detail.get("title", "")
                actual_source = "api"
        except Exception as e:
            if mode == "api":
                raise RuntimeError(f"动态接口 (getDocPageDetail) 获取失败: {e}")
            print(f"[DEBUG] getDocPageDetail fallback to html for {doc_id}: {e}")

    # 2. 若正文仍为空且允许 html 抓取（auto 回退或明确指定 html 模式）
    if not text:
        if mode == "api":
            raise RuntimeError(f"动态接口 (getDocPageDetail) 未返回有效文档正文 (doc_id={doc_id})")

        actual_source = "html"
        req = urllib.request.Request(
            doc_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate",
            },
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw_bytes = resp.read()
            try:
                decompressed = gzip.decompress(raw_bytes)
            except Exception:
                decompressed = raw_bytes
            text = decompressed.decode("utf-8", errors="ignore")

    # 3. 提取/补充标题
    if not title:
        title_m = re.search(r"<h[1-2][^>]*>(.*?)</h[1-2]>", text, re.DOTALL)
        title = html.unescape(re.sub(r"<[^>]+>", "", title_m.group(1))).strip() if title_m else ""

    # 4. 提取功能说明 (真实正文内容，逐项解析)
    desc_m = re.search(r"<h[1-6][^>]*>(?:(?!</h[1-6]>).)*?(?:功能说明|接口说明|接口描述).*?</h[1-6]>(.*?)(?:<h[1-6]|\Z)", text, re.DOTALL)
    if desc_m:
        d = re.sub(r"</?(p|div|br|li|ul|ol)[^>]*>", "\n", desc_m.group(1))
        d_clean = html.unescape(re.sub(r"<[^>]+>", " ", d)).strip()
        raw_lines = [
            re.sub(r"^[•\-\*\s]+", "", line.strip())
            for line in d_clean.split("\n")
            if line.strip()
            and not line.strip().startswith("!function")
            and line.strip() not in ("功能说明", "接口说明", "接口描述")
            and not line.strip().startswith(("以下视频", "﻿"))
        ]
        merged_lines = []
        skip_next = False
        for idx, line in enumerate(raw_lines):
            if skip_next:
                skip_next = False
                continue
            if line in ("注意", "注意：", "说明", "说明：", "提示", "提示：") and idx + 1 < len(raw_lines):
                tag = line.rstrip("：:")
                merged_lines.append(f"{tag}：{raw_lines[idx + 1].lstrip('：:')}")
                skip_next = True
            else:
                merged_lines.append(line)
        overview = "\n".join(merged_lines)

    # 兜底：若正文中无功能说明标题，使用 API description 作为备用
    if not overview and detail and detail.get("description"):
        overview = detail.get("description", "")

    # 3. 频率限制
    rate_limit = ""
    rate_m = re.search(r"<h[1-6][^>]*>(?:(?!</h[1-6]>).)*?(?:频率|限频|最高调用).*?</h[1-6]>(.*?)(?:<h[1-6]|\Z)", text, re.DOTALL)
    if rate_m:
        r_clean = html.unescape(re.sub(r"<[^>]+>", " ", rate_m.group(1))).strip()
        rate_limit = " ".join(r_clean.split())

    # 4. 遍历文档节点序列：Heading、Table、Pre Code
    elem_pattern = re.compile(
        r"(<h[2-5][^>]*>(.*?)</h[2-5]>)|(<table[^>]*>(.*?)</table>)|(<pre[^>]*>(.*?)</pre>)",
        re.DOTALL,
    )

    current_major_section = ""
    current_sub_heading = ""
    url_sample = ""
    url_params = []
    req_fields = []
    resp_fields = []
    err_codes = []
    req_examples = []
    resp_examples = []
    callbacks = []
    notes = []
    last_pre_end = 0

    for m in elem_pattern.finditer(text):
        if m.group(1):
            h_text = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
            current_sub_heading = h_text
            if any(k in h_text for k in [
                "请求包", "请求示例", "请求 URL", "请求参数", "最高调用频率",
                "功能说明", "应答包", "错误码", "回调", "输入参数", "输出参数",
                "输入示例", "输出示例", "示例"
            ]):
                current_major_section = h_text
        elif m.group(3):
            # 表格解析
            tbl_html = m.group(4)
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tbl_html, re.DOTALL)
            if not rows:
                continue

            headers = []
            parsed_rows = []
            for r_idx, row in enumerate(rows):
                cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.DOTALL)
                clean_cells = [
                    html.unescape(re.sub(r"<[^>]+>", "", c)).strip().replace("\n", " ")
                    for c in cells
                ]
                if r_idx == 0:
                    headers = clean_cells
                else:
                    if len(clean_cells) == len(headers):
                        parsed_rows.append(dict(zip(headers, clean_cells)))
                    else:
                        parsed_rows.append({f"col_{i}": c for i, c in enumerate(clean_cells)})

            def get_cell(row_dict, *aliases):
                for a in aliases:
                    if a in row_dict and row_dict[a] is not None:
                        val = str(row_dict[a]).strip()
                        if val:
                            return val
                for k, v in row_dict.items():
                    for a in aliases:
                        if a in k and v is not None:
                            val = str(v).strip()
                            if val:
                                return val
                return ""

            h_str = " ".join(headers)
            heading_ctx = f"{current_major_section} {current_sub_heading}"

            has_err = any(k in h_str for k in ["错误码", "ErrorCode", "code", "Code"])
            has_field = any(k in h_str for k in ["字段", "参数", "属性名", "名称", "Field", "Name", "key"])
            has_type = any(k in h_str for k in ["类型", "数据类型", "Type"])
            has_req = any(k in h_str for k in ["属性", "必填", "必选", "是否必填", "Required"])

            if has_err:
                for r in parsed_rows:
                    code = get_cell(r, "错误码", "ErrorCode", "code")
                    desc = get_cell(r, "含义说明", "描述", "说明", "含义")
                    sol = get_cell(r, "处理建议", "解决建议", "排查建议", "建议")
                    if code and code != "错误码":
                        err_codes.append({"code": code, "description": desc, "solution": sol})
            elif ("URL" in heading_ctx or "请求参数" in heading_ctx or "参数说明" in heading_ctx) and not has_type and (len(headers) <= 2 or "参数" in h_str):
                for r in parsed_rows:
                    name = get_cell(r, "参数", "参数名", "字段", "字段名")
                    desc = get_cell(r, "说明", "描述", "含义")
                    if name and name not in ("参数", "字段", "参数名", "字段名"):
                        url_params.append({"name": name, "description": desc})
            elif has_field or has_type:
                is_resp = any(k in heading_ctx for k in ["输出参数", "输出", "应答", "回包", "返回", "响应", "Response", "response"])
                is_req = any(k in heading_ctx for k in ["输入参数", "输入", "请求包", "请求字段", "请求参数", "Request", "request"])

                if is_resp or (not is_req and not has_req and len(req_fields) > 0):
                    for r in parsed_rows:
                        name = get_cell(r, "字段名", "字段", "参数名", "参数", "属性名", "名称", "参数名称")
                        typ = get_cell(r, "类型", "数据类型", "Type")
                        desc = get_cell(r, "说明", "描述", "含义", "含义说明")
                        if name and name not in ("字段", "字段名", "参数", "参数名", "参数名称"):
                            clean_name = name[:-2] if name.endswith(".N") else name
                            resp_fields.append({"name": clean_name, "type": typ, "description": desc})
                else:
                    for r in parsed_rows:
                        name = get_cell(r, "字段名", "字段", "参数名", "参数", "属性名", "名称", "参数名称")
                        typ = get_cell(r, "类型", "数据类型", "Type")
                        req_val = get_cell(r, "必填", "是否必填", "属性", "必选", "Required")
                        desc = get_cell(r, "说明", "描述", "含义", "含义说明")
                        if req_val in ("是", "true", "True", "Y", "YES"):
                            req_val = "必填"
                        elif req_val in ("否", "false", "False", "N", "NO"):
                            req_val = "选填"
                        if name in ("Action", "Version", "Region") or "公共参数" in desc:
                            req_val = "公共参数"
                        if name and name not in ("字段", "字段名", "参数", "参数名", "参数名称"):
                            clean_name = name[:-2] if name.endswith(".N") else name
                            field_item: dict[str, Any] = {"name": clean_name, "type": typ, "required": req_val, "description": desc}
                            if name != clean_name:
                                field_item["aliases"] = [name]
                            req_fields.append(field_item)
        elif m.group(5):
            # Pre 代码块
            pr_html = m.group(6)
            p_clean = re.sub(r"</?(div|p|br|li|tr)[^>]*>", "\n", pr_html)
            clean = html.unescape(re.sub(r"<[^>]+>", "", p_clean)).strip()
            if not clean:
                continue

            # 智能提取当前代码块的所属场景标题（分析与上一个代码块之间的文本）
            pre_start = m.start()
            between = text[last_pre_end:pre_start]
            last_pre_end = m.end()

            candidate_tags = re.findall(r"<(h[2-6]|b|strong)[^>]*>(.*?)</\1>", between, re.DOTALL)
            context_title = ""
            for tag_name, content in candidate_tags:
                clean_t = html.unescape(re.sub(r"<[^>]+>", "", content)).strip()
                clean_t = re.sub(r"^[0-9一二三四五六七八九十\.\、\s\-\*]+", "", clean_t).strip().rstrip("：:")
                # 排除通用章节和提示性标题
                if (
                    clean_t
                    and len(clean_t) <= 80
                    and clean_t not in (
                        "注意", "说明", "提示", "请求包示例", "请求包", "应答包",
                        "应答包示例", "JSON", "HTTP", "请求 URL 示例", "请求参数说明",
                        "请求频率限制", "请求包字段说明", "适用的群组类型", "功能说明",
                        "接口调用说明", "参数说明", "示例说明", "回包字段说明", "返回结果",
                        "调用频率限制", "最高调用频率", "频率限制", "调用频率"
                    )
                    and not clean_t.startswith(("http", "https", "curl", "{", "["))
                ):
                    context_title = clean_t

            ctx = f"{current_major_section} {current_sub_heading}"
            is_resp_block = (
                any(k in ctx for k in ["输出示例", "输出", "应答", "回包", "返回", "Response", "response"])
                or "\"Response\":" in clean or "'Response':" in clean
                or "\"RequestId\":" in clean or "'RequestId':" in clean
                or any(k in clean for k in ("ActionStatus", "ErrorCode", "ErrorInfo"))
            ) and not any(k in current_sub_heading for k in ["输入示例", "输入参数", "请求包", "请求示例"])

            if is_resp_block:
                title_ex = context_title or (
                    current_sub_heading if any(k in current_sub_heading for k in ["输出", "应答", "回包"]) else "官方输出示例"
                )
                if not context_title:
                    if "FAIL" in clean or ("ErrorCode" in clean and '"ErrorCode": 0' not in clean):
                        title_ex = "失败应答示例"
                    elif "OK" in clean or '"ErrorCode": 0' in clean or '"RequestId":' in clean:
                        title_ex = "成功应答示例"
                resp_examples.append({
                    "title": title_ex,
                    "content": clean,
                    "clean_json": clean_json_str(clean)
                })
            else:
                if clean.startswith("http") and "?" in clean:
                    url_sample = clean
                    body_from_url = parse_url_to_body(clean)
                    if body_from_url:
                        req_examples.append({
                            "title": context_title or current_sub_heading or "官方基础请求参数",
                            "content": clean,
                            "clean_json": json.dumps(body_from_url, ensure_ascii=False, indent=2)
                        })
                else:
                    title_ex = context_title or (
                        current_sub_heading if current_sub_heading not in ("请求包示例", "请求包", "输入示例") else f"示例 {len(req_examples) + 1}"
                    )
                    req_examples.append({
                        "title": title_ex,
                        "content": clean,
                        "clean_json": clean_json_str(clean)
                    })

    # 提取回调列表
    cb_m = re.search(r"<h[1-6][^>]*>(?:(?!</h[1-6]>).)*?(?:回调).*?</h[1-6]>(.*?)(?:<h[1-6]|\Z)", text, re.DOTALL)
    if cb_m:
        cb_links = re.findall(r"<a[^>]*>(.*?)</a>", cb_m.group(1))
        for cl in cb_links:
            clean_l = html.unescape(re.sub(r"<[^>]+>", "", cl)).strip()
            if clean_l and "回调" in clean_l and clean_l not in callbacks:
                callbacks.append(clean_l)

    return {
        "doc_id": doc_id,
        "api_id": api_id,
        "title": title,
        "doc_url": doc_url,
        "sync_time": get_current_time_str(),
        "status": "synced",
        "source_mode": actual_source,  # 'html' | 'api'
        "requested_mode": mode,        # 'auto' | 'html' | 'api'
        "overview": overview,
        "rate_limit": rate_limit,
        "request_url_sample": url_sample,
        "url_params": url_params,
        "request_body_fields": req_fields,
        "request_examples": req_examples,
        "response_body_fields": resp_fields,
        "response_examples": resp_examples,
        "error_codes": err_codes,
        "callbacks": callbacks,
        "notes": notes,
    }


def save_doc(doc_data: dict) -> str:
    doc_id = doc_data.get("doc_id")
    if not doc_id:
        raise ValueError("Missing doc_id in doc_data")
    filepath = get_doc_path(doc_id)
    atomic_write_json(filepath, doc_data, mode=0o600)
    return filepath


def get_doc_by_id(doc_id: int) -> Optional[dict]:
    filepath = get_doc_path(doc_id)
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


class DocSyncManager:
    """官方文档同步管理器。

    文档正文继续是可重建的文件缓存；同步任务本身使用 ``sync_jobs`` 保存
    任务句柄、租约、进度和取消标志。因此两个 Uvicorn 进程查询到的是同一
    状态，进程重启后也不会把另一个进程的任务误报成空闲。
    """

    KIND = "docs_batch"

    def __init__(self, task_manager: SyncTaskManager | None = None):
        self._task_manager = task_manager or SyncTaskManager(persistent=True)
        self._owner_id = "local"

    @staticmethod
    def _empty_status() -> dict[str, Any]:
        return {
            "task_id": None,
            "status": "IDLE",
            "is_running": False,
            "total": 0,
            "completed": 0,
            "success_count": 0,
            "error_count": 0,
            "percent": 0,
            "current_name": "",
            "current_doc_id": 0,
            "start_time": None,
            "end_time": None,
            "errors": [],
            "cancel_requested": False,
        }

    def get_status(self) -> dict[str, Any]:
        durable = self._task_manager.get_status(
            self.KIND,
            owner_id=None,
            issuer="",
            tenant_id="",
            is_admin=True,
        )
        if not durable.get("task_id"):
            return self._empty_status()
        progress = durable.get("progress") or {}
        return {
            "task_id": durable.get("task_id"),
            "status": durable.get("status"),
            "is_running": bool(durable.get("is_running")),
            "total": int(progress.get("total") or 0),
            "completed": int(progress.get("completed") or 0),
            "success_count": int(progress.get("success_count") or 0),
            "error_count": int(progress.get("error_count") or 0),
            "percent": int(progress.get("percent") or 0),
            "current_name": progress.get("current_name") or "",
            "current_doc_id": progress.get("current_doc_id") or 0,
            "start_time": progress.get("start_time") or durable.get("started_at"),
            "end_time": progress.get("end_time") or durable.get("finished_at"),
            "errors": list(progress.get("errors") or []),
            "cancel_requested": bool(
                durable.get("cancel_requested")
                or progress.get("cancel_requested")
            ),
            "result": durable.get("result"),
            "error": durable.get("error"),
            "attempt": int(durable.get("attempt") or 0),
        }

    def get_doc_for_api(self, api_meta: dict) -> dict:
        """读取本地文档缓存，未同步时返回可用的参数骨架。"""

        doc_id = api_meta.get("doc_id")
        api_id = str(api_meta.get("api_id") or "")
        name = api_meta.get("name", "")
        doc_url = api_meta.get("doc_url", "")
        desc = api_meta.get("description", "")
        sample_payload = api_meta.get("sample_payload", {})
        fields = api_meta.get("fields", [])

        if doc_id:
            saved = get_doc_by_id(doc_id)
            if saved:
                saved["api_id"] = api_id
                return saved

        req_fields = [
            {
                "name": f.get("name"),
                "type": f.get("type", "string"),
                "required": "必填" if f.get("required") else "选填",
                "description": f.get("help_text") or f.get("label") or "",
            }
            for f in fields
        ]
        req_examples = []
        if sample_payload:
            payload_str = json.dumps(sample_payload, ensure_ascii=False, indent=2)
            req_examples.append({
                "title": "官方基础示例",
                "content": payload_str,
                "clean_json": payload_str,
            })

        service_type = api_meta.get("service_type", "im") or "im"
        if service_type == "im":
            default_url_params = [
                {"name": "sdkappid", "description": "创建应用时分配的 SDKAppID"},
                {"name": "identifier", "description": "必须为 App 管理员账号"},
                {"name": "usersig", "description": "管理员 UserSig 签名"},
                {"name": "random", "description": "32位无符号整数随机数"},
                {"name": "contenttype", "description": "固定为 json"},
            ]
            default_resp_fields = [
                {"name": "ActionStatus", "type": "String", "description": "请求处理结果：OK 或 FAIL"},
                {"name": "ErrorCode", "type": "Integer", "description": "错误码：0为成功，非0为失败"},
                {"name": "ErrorInfo", "type": "String", "description": "详细错误信息"},
            ]
            default_resp_examples = [{
                "title": "标准成功响应",
                "content": '{\n  "ActionStatus": "OK",\n  "ErrorCode": 0,\n  "ErrorInfo": ""\n}',
                "clean_json": '{\n  "ActionStatus": "OK",\n  "ErrorCode": 0,\n  "ErrorInfo": ""\n}',
            }]
            default_err_codes = [
                {"code": "0", "description": "操作成功", "solution": "无需处理"},
                {"code": "60002", "description": "UserSig 签名过期或无效", "solution": "请重新生成管理员 UserSig"},
                {"code": "70001", "description": "SecretKey 不匹配或应用未配置", "solution": "核对控制台密钥配置"},
                {"code": "90001", "description": "请求频率超限 (触发 QPS 限制)", "solution": "降低调用速率或联系腾讯云提频"},
            ]
        else:
            action_name = api_meta.get("action") or api_meta.get("command") or ""
            version_val = api_meta.get("version", "2018-08-01")
            default_url_params = [
                {"name": "X-TC-Action", "description": f"接口 Action 名称 (如 {action_name})"},
                {"name": "X-TC-Version", "description": f"接口版本号 (如 {version_val})"},
                {"name": "X-TC-Timestamp", "description": "当前 UNIX 时间戳 (秒)"},
                {"name": "Authorization", "description": "TC3-HMAC-SHA256 签名凭据串"},
                {"name": "X-TC-Region", "description": "地域参数 (如 ap-guangzhou)"},
            ]
            default_resp_fields = [
                {"name": "Response.RequestId", "type": "String", "description": "唯一请求 ID，每次请求都会返回"},
                {"name": "Response.Error", "type": "Object", "description": "调用失败时的错误信息对象"},
                {"name": "Response.Error.Code", "type": "String", "description": "错误码，如 FailedOperation"},
                {"name": "Response.Error.Message", "type": "String", "description": "错误信息描述"},
            ]
            default_resp_examples = [{
                "title": "标准 API 3.0 成功响应示例",
                "content": '{\n  "Response": {\n    "RequestId": "d98129a0-8113-41c3-bb02-c322b918731f"\n  }\n}',
                "clean_json": '{\n  "Response": {\n    "RequestId": "d98129a0-8113-41c3-bb02-c322b918731f"\n  }\n}',
            }]
            default_err_codes = [
                {"code": "InvalidParameter", "description": "参数错误，请检查入参类型与取值", "solution": "核对入参字段"},
                {"code": "AuthFailure.SignatureFailure", "description": "签名失败，SecretId 或 SecretKey 不匹配", "solution": "检查环境配置中的 API 3.0 密钥"},
                {"code": "RequestLimitExceeded", "description": "请求频次超限 (触发 QPS 限制)", "solution": "降低并发频率或申请提频"},
            ]

        return {
            "doc_id": doc_id or 0,
            "api_id": api_id,
            "title": name,
            "doc_url": doc_url,
            "sync_time": None,
            "status": "not_synced",
            "overview": desc,
            "rate_limit": "",
            "request_url_sample": "",
            "url_params": default_url_params,
            "request_body_fields": req_fields,
            "request_examples": req_examples,
            "response_body_fields": default_resp_fields,
            "response_examples": default_resp_examples,
            "error_codes": default_err_codes,
            "callbacks": [],
            "notes": [],
        }

    def sync_single(
        self,
        doc_id: int,
        api_id: str = "",
        doc_url: str = "",
        mode: str = "auto",
    ) -> dict:
        parsed = parse_official_doc(doc_id, api_id, doc_url, mode=mode)
        save_doc(parsed)
        return parsed

    def start_batch_sync(
        self,
        apis_list: list[dict],
        force: bool = False,
        mode: str = "auto",
    ) -> dict:
        total_items = len(apis_list)
        initial_progress: dict[str, Any] = {
            "total": total_items,
            "mode": mode,
            "completed": 0,
            "success_count": 0,
            "error_count": 0,
            "percent": 0,
            "current_name": "准备中...",
            "current_doc_id": 0,
            "start_time": get_current_time_str(),
            "end_time": None,
            "errors": [],
            "cancel_requested": False,
        }

        def operation_factory(task_id: str):
            def operation() -> dict[str, Any]:
                progress_lock = threading.Lock()
                state: dict[str, Any] = dict(initial_progress)
                state["errors"] = []

                def publish() -> None:
                    with progress_lock:
                        snapshot = dict(state)
                        snapshot["errors"] = list(state["errors"][-100:])
                    self._task_manager.update_progress(task_id, snapshot)

                def cancelled() -> bool:
                    return self._task_manager.is_cancel_requested(task_id)

                publish()

                def process_item(item: dict[str, Any]) -> None:
                    if cancelled():
                        return
                    doc_id = item.get("doc_id")
                    api_id = item.get("api_id")
                    name = item.get("name", "")
                    doc_url = item.get("doc_url", "")
                    if not doc_id:
                        with progress_lock:
                            state["completed"] += 1
                        publish()
                        return

                    if not force and os.path.exists(get_doc_path(doc_id)):
                        with progress_lock:
                            state["completed"] += 1
                            state["success_count"] += 1
                            state["current_name"] = f"{name} (已跳过本地已有)"
                            state["current_doc_id"] = doc_id
                            state["percent"] = int(
                                state["completed"] / total_items * 100
                            ) if total_items else 100
                        publish()
                        return

                    with progress_lock:
                        state["current_name"] = name
                        state["current_doc_id"] = doc_id
                    publish()

                    try:
                        parsed = parse_official_doc(
                            doc_id,
                            api_id,
                            doc_url,
                            mode=mode,
                        )
                        save_doc(parsed)
                        with progress_lock:
                            state["success_count"] += 1
                    except Exception as exc:  # noqa: BLE001 - task reports item error
                        with progress_lock:
                            state["error_count"] += 1
                            state["errors"].append(
                                {
                                    "doc_id": doc_id,
                                    "name": name,
                                    "error": str(exc)[:500],
                                }
                            )

                    with progress_lock:
                        state["completed"] += 1
                        state["percent"] = int(
                            state["completed"] / total_items * 100
                        ) if total_items else 100
                    publish()
                    time.sleep(0.08)

                executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)
                futures = [executor.submit(process_item, item) for item in apis_list]
                try:
                    for future in concurrent.futures.as_completed(futures):
                        future.result()
                        if cancelled():
                            break
                finally:
                    if cancelled():
                        for future in futures:
                            future.cancel()
                    executor.shutdown(wait=True, cancel_futures=True)

                if cancelled():
                    with progress_lock:
                        state["cancel_requested"] = True
                        state["end_time"] = get_current_time_str()
                        state["current_name"] = "已取消"
                    publish()
                    raise SyncTaskCancelled(state)

                with progress_lock:
                    state["end_time"] = get_current_time_str()
                    state["current_name"] = "已完成"
                publish()
                return state

            return operation

        result = self._task_manager.start(
            self.KIND,
            lambda: None,
            operation_factory=operation_factory,
            initial_progress=initial_progress,
            owner_id=self._owner_id,
            issuer="",
            tenant_id="",
            is_admin=True,
        )
        result["sync_status"] = self.get_status()
        if result.get("status") == "started":
            result["message"] = f"已启动 {total_items} 项文档异步后台同步任务"
        elif result.get("status") == "already_running":
            result["message"] = "批量同步任务正在后台运行中"
        return result

    def cancel_batch_sync(self) -> dict:
        result = self._task_manager.cancel(
            self.KIND,
            owner_id=self._owner_id,
            issuer="",
            tenant_id="",
            is_admin=True,
        )
        result["sync_status"] = self.get_status()
        return result

    def get_summary(self, apis_list: list[dict]) -> dict:
        total = len(apis_list)
        synced_count = 0
        last_sync_time = None

        for item in apis_list:
            doc_id = item.get("doc_id")
            if doc_id and os.path.exists(get_doc_path(doc_id)):
                synced_count += 1
                try:
                    mtime = datetime.fromtimestamp(
                        os.path.getmtime(get_doc_path(doc_id)),
                        tz=BEIJING_TZ,
                    )
                    mtime_str = mtime.strftime("%Y-%m-%d %H:%M:%S")
                    if not last_sync_time or mtime_str > last_sync_time:
                        last_sync_time = mtime_str
                except Exception:
                    pass

        return {
            "total_apis": total,
            "synced_count": synced_count,
            "not_synced_count": max(0, total - synced_count),
            "synced_percent": int((synced_count / total) * 100) if total > 0 else 0,
            "last_sync_time": last_sync_time,
            "is_running": self.get_status()["is_running"],
        }

doc_sync_manager = DocSyncManager()
