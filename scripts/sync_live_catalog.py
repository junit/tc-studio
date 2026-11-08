# -*- coding: utf-8 -*-
"""
腾讯云直播 (CSS) 官方全量 API 目录与参数定义动态拉取脚本
从官方文档产品 267 (云直播) 的 API 概览 (doc 20456) 提取全量 260+ 接口元数据，
并并发抓取各接口参数定义生成 core/live_full_catalog.json
"""

import gzip
import json
import os
import random
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from core.file_storage import atomic_write_json

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUTPUT_PATH = os.path.join(BASE_DIR, "core", "live_full_catalog.json")
CACHE_DIR = os.path.join(BASE_DIR, "storage", "docs_cache", "live")
os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
try:
    os.chmod(CACHE_DIR, 0o700)
except OSError:
    pass

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept-Encoding": "gzip, deflate",
}


def fetch_doc_detail(doc_id: str, max_retries: int = 3) -> dict:
    cache_file = os.path.join(CACHE_DIR, f"{doc_id}.json")
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    url = "https://cloud.tencent.com/document/cgi/document/getDocPageDetail"
    payload = {
        "action": "getDocPageDetail",
        "payload": {
            "id": str(doc_id),
            "lang": "zh",
            "isPreview": False,
            "isFromClient": True,
        },
    }

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=HEADERS)
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass
                data = json.loads(raw.decode("utf-8"))
                content = data.get("data", {}).get("content", {})
                if content:
                    atomic_write_json(cache_file, content, mode=0o600)
                return content
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(0.3 * (attempt + 1) + random.uniform(0.1, 0.3))
            else:
                pass
    return {}


def parse_fields_from_html(body: str) -> tuple:
    """从接口正文 HTML 解析描述、参数列表及示例入参"""
    desc_m = re.search(r'接口描述.*?<p>(.*?)</p>', body, re.DOTALL)
    desc = re.sub(r'<.*?>', '', desc_m.group(1)).strip() if desc_m else ""

    fields = []
    sample_payload = {}

    m = re.search(r'输入参数.*?<table.*?>(.*?)</table>', body, re.DOTALL)
    if m:
        rows = re.findall(r'<tr>(.*?)</tr>', m.group(1), re.DOTALL)
        for r in rows:
            tds = [re.sub(r'<.*?>', '', td).strip() for td in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', r, re.DOTALL)]
            if len(tds) >= 4 and tds[0] not in ("Action", "Version", "Region", "参数名称"):
                p_name = tds[0]
                p_req = (tds[1] == "是")
                p_type_orig = tds[2].strip()
                p_type_raw = p_type_orig.lower()
                p_desc = tds[3]

                t = "string"
                if "array" in p_type_raw or "list" in p_type_raw or p_type_orig.endswith("[]"):
                    t = "list"
                elif p_type_raw in ("boolean", "bool"):
                    t = "boolean"
                elif "int" in p_type_raw or "float" in p_type_raw or "double" in p_type_raw or p_type_raw == "number":
                    t = "number"
                elif "object" in p_type_raw or "dict" in p_type_raw or "json" in p_type_raw or "map" in p_type_raw:
                    t = "json"
                elif p_type_orig and p_type_orig[0].isupper() and p_type_orig not in ("String", "Integer", "Float", "Boolean"):
                    t = "json"

                # 提取示例值
                sample_m = re.search(r'示例值[：:]\s*([^\s<"\'\n]+)', p_desc)
                placeholder = sample_m.group(1) if sample_m else ""

                val = None
                if p_req:
                    if t == "number":
                        val = int(placeholder) if placeholder.isdigit() else 0
                    elif t == "boolean":
                        val = placeholder.lower() == "true"
                    elif t == "list":
                        val = [placeholder] if placeholder else []
                    elif t == "json":
                        val = {}
                    else:
                        val = placeholder or "sample_value"
                    sample_payload[p_name] = val

                fields.append({
                    "name": p_name,
                    "label": p_name,
                    "type": t,
                    "required": p_req,
                    "placeholder": placeholder,
                    "help_text": p_desc[:160]
                })

    return desc, fields, sample_payload


def sync_all_live_apis():
    print("=" * 60)
    print("🚀 正在从腾讯云官方拉取云直播 (CSS) 全量 API 概览...")
    overview_doc = fetch_doc_detail("20456")
    body = overview_doc.get("body", "")

    sections = re.findall(r'<h[23][^>]*>(.*?)</h[23]>.*?<table.*?>(.*?)</table>', body, re.DOTALL)
    print(f"找到 {len(sections)} 个业务分类模块")

    raw_api_list = []
    for sec_title, table_html in sections:
        sec_clean = re.sub(r'<.*?>', '', sec_title).strip()
        # 清理 "相关接口" 后缀
        if sec_clean.endswith("相关接口"):
            subcategory = sec_clean[:-4]
        else:
            subcategory = sec_clean

        rows = re.findall(r'<tr>(.*?)</tr>', table_html, re.DOTALL)
        for r in rows:
            tds = re.findall(r'<td[^>]*>(.*?)</td>', r, re.DOTALL)
            if len(tds) >= 2:
                action_m = re.search(r'>([A-Za-z0-9_]+)<', tds[0])
                url_m = re.search(r'href=[\"\']([^\"\']+)[\"\']', tds[0])
                action = action_m.group(1) if action_m else re.sub(r'<.*?>', '', tds[0]).strip()
                title = re.sub(r'<.*?>', '', tds[1]).strip()
                qps_str = re.sub(r'<.*?>', '', tds[2]).strip() if len(tds) >= 3 else ""

                doc_id = ""
                doc_url = "https://cloud.tencent.com/document/product/267/20455"
                if url_m:
                    href = url_m.group(1)
                    doc_url = "https://cloud.tencent.com" + href
                    id_m = re.search(r'/(\d+)$', href)
                    if id_m:
                        doc_id = id_m.group(1)

                raw_api_list.append({
                    "action": action,
                    "title": title,
                    "subcategory": subcategory,
                    "doc_id": doc_id,
                    "doc_url": doc_url,
                    "qps": qps_str
                })

    print(f"共发现 {len(raw_api_list)} 个官方云直播 API，开始并发拉取接口参数与描述详情...")

    def fetch_api_detail(item):
        doc_id = item.get("doc_id")
        desc = ""
        fields = []
        sample_payload = {}
        if doc_id:
            content = fetch_doc_detail(doc_id)
            if content.get("body"):
                desc, fields, sample_payload = parse_fields_from_html(content["body"])
                if not desc and content.get("description"):
                    desc = content["description"]

        action = item["action"]
        title = item["title"]
        subcategory = item["subcategory"]

        return {
            "api_id": f"live_{action}",
            "name": title,
            "service_type": "live",
            "service_name": "live",
            "command": action,
            "action": action,
            "version": "2018-08-01",
            "endpoint": "live.tencentcloudapi.com",
            "category": "云直播 (CSS)",
            "subcategory": subcategory,
            "path": ["云直播 (CSS)", subcategory],
            "doc_id": int(doc_id) if doc_id and doc_id.isdigit() else 20455,
            "doc_url": item["doc_url"],
            "description": desc or f"腾讯云直播 {title} ({action}) 接口",
            "qps": item.get("qps", "20"),
            "fields": fields,
            "sample_payload": sample_payload
        }

    t0 = time.time()
    # 使用稳健的并发数
    with ThreadPoolExecutor(max_workers=6) as executor:
        catalog = list(executor.map(fetch_api_detail, raw_api_list))

    print(f"✅ 全量 {len(catalog)} 项云直播 API 参数抓取完成，耗时 {round(time.time() - t0, 2)} 秒！")

    atomic_write_json(OUTPUT_PATH, catalog, mode=0o644)

    print(f"🎉 已成功保存至 {OUTPUT_PATH}")
    return catalog


if __name__ == "__main__":
    sync_all_live_apis()
