# -*- coding: utf-8 -*-
"""
腾讯云 IM 官方目录树与接口元数据动态获取引擎
通过抓取官方站点 window.__staticRouterHydrationData 及 getDocPageDetail 接口，
实现全量目录层级与接口元数据的实时动态拉取、解析与本地热重载。
"""

from datetime import datetime, timezone, timedelta
import gzip
import html
import json
import os
import re
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

from core.file_storage import atomic_write_json
from core.registry import registry
from core.runtime_paths import (
    package_catalog_path,
    runtime_catalog_path,
    runtime_storage_dir,
)

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CATALOG_PATH = str(runtime_catalog_path("im"))
STATIC_CATALOG_PATH = str(package_catalog_path("im"))
STORAGE_DIR = str(runtime_storage_dir())
os.makedirs(STORAGE_DIR, mode=0o700, exist_ok=True)

BEIJING_TZ = timezone(timedelta(hours=8))


def get_current_time_str() -> str:
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def fetch_doc_detail_api(doc_id: int, max_retries: int = 3) -> Optional[dict]:
    """
    通过官方 getDocPageDetail 接口直接拉取页面元数据与正文 JSON（支持针对 567 等频控超时的重试机制）
    """
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

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://cloud.tencent.com",
            "Referer": f"https://cloud.tencent.com/document/api/267/{doc_id}",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Encoding": "gzip, deflate",
        },
    )

    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass
                data = json.loads(raw.decode("utf-8"))
                if data.get("code") == 0:
                    return data.get("data", {}).get("content", {})
                elif data.get("code") == 301:
                    redirect_url = data.get("detail", {}).get("redirectUrl", "")
                    m = re.search(r"/(\d+)$", redirect_url)
                    if m and m.group(1) != str(doc_id):
                        target_id = int(m.group(1))
                        if target_id in (20456, 1519, 1520):
                            return {
                                "title": f"已下线接口 (原文档 {doc_id})",
                                "body": f"<h2>接口说明</h2><p>该接口已在腾讯云官方下线，官方文档已重定向至 API 概览页面: <a href='{redirect_url}'>{redirect_url}</a>。</p>",
                                "is_offline": True,
                            }
                        if max_retries > 1:
                            return fetch_doc_detail_api(target_id, max_retries=max_retries - 1)
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(0.5 * (attempt + 1))
            else:
                print(f"[WARN] Failed to fetch doc detail for {doc_id} after {max_retries} attempts: {e}")
    return None


def fetch_official_hydration_tree() -> Tuple[dict, List[dict]]:
    """
    访问官方文档页面 1519，动态提取 window.__staticRouterHydrationData，
    解析出官方最新的 服务端 API -> REST API 全量多级目录树
    """
    url = "https://cloud.tencent.com/document/product/269/1519"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        },
    )

    with urllib.request.urlopen(req, timeout=15) as resp:
        raw_bytes = resp.read()
        try:
            raw_bytes = gzip.decompress(raw_bytes)
        except Exception:
            pass
        text = raw_bytes.decode("utf-8", errors="ignore")

    # 提取 window.__staticRouterHydrationData = JSON.parse(...)
    m = re.search(r"window\.__staticRouterHydrationData\s*=\s*JSON\.parse\((.*?)\);", text, re.DOTALL)
    if not m:
        raise ValueError("未能从页面中定位到 window.__staticRouterHydrationData")

    hydration_data = json.loads(json.loads(m.group(1).strip()))
    loader_data = hydration_data.get("loaderData", {})
    product_data = loader_data.get("product", {}).get("data", {})
    catalogue_list = product_data.get("sidebar", {}).get("catalogue", {}).get("list", [])

    # 定位服务端 API (id: 33099)
    server_api = next((item for item in catalogue_list if item.get("id") == 33099), None)
    if not server_api:
        raise ValueError("官方目录结构中未找到 '服务端 API' (id: 33099)")

    # 定位 REST API (id: 1518)
    rest_api = next((item for item in server_api.get("children", []) if item.get("id") == 1518), None)
    if not rest_api:
        raise ValueError("服务端 API 中未找到 'REST API' (id: 1518)")

    # 递归提取页面列表及完整路径
    pages: List[dict] = []

    def traverse(node: dict, current_path: List[str]):
        ntype = node.get("type")
        title = node.get("title", "").strip()
        doc_id = node.get("id")
        children = node.get("children", [])

        if ntype == "page":
            # 排除概述类页面 1519(REST API 简介), 1520(REST API 接口列表)
            if doc_id not in (1519, 1520):
                pages.append({
                    "doc_id": doc_id,
                    "title": title,
                    "path": list(current_path),
                    "doc_url": f"https://cloud.tencent.com/document/product/269/{doc_id}",
                })
        elif ntype == "directory":
            new_path = current_path + [title] if title != "REST API" else current_path
            for child in children:
                traverse(child, new_path)

    for item in rest_api.get("children", []):
        traverse(item, [])

    _save_tree_snapshot_if_usable(rest_api, pages)

    return rest_api, pages


def _save_tree_snapshot_if_usable(tree: dict, pages: List[dict]) -> None:
    """Publish only a non-empty official tree snapshot."""

    # 空页面结果可能来自官方页面结构变化或解析退化；不要覆盖共享目录中
    # 上一次可用的快照，否则其他进程会立刻看到一棵空树。
    if not pages:
        return
    snapshot_path = os.path.join(STORAGE_DIR, "official_live_tree.json")
    atomic_write_json(snapshot_path, tree, mode=0o600)


def _load_existing_catalog() -> list[dict]:
    """Load a usable runtime catalog, falling back to the package copy."""

    seen: set[str] = set()
    for candidate in (CATALOG_PATH, STATIC_CATALOG_PATH):
        if candidate in seen or not os.path.exists(candidate):
            continue
        seen.add(candidate)
        try:
            with open(candidate, "r", encoding="utf-8") as stream:
                loaded = json.load(stream)
            if (
                isinstance(loaded, list)
                and loaded
                and all(isinstance(item, dict) for item in loaded)
            ):
                return loaded
            print(f"[WARN] Ignoring unusable catalog {candidate}")
        except (OSError, ValueError) as exc:
            print(f"[WARN] Failed to load catalog {candidate}: {exc}")
    return []


def sync_catalog_from_live_tree() -> dict:
    """
    执行官方最新目录树的动态同步与本地目录热更新：
    1. 动态抓取官方实时目录树；
    2. 对比本地现有目录，更新分类层级与接口名称；
    3. 若发现全新接口，通过 getDocPageDetail 抓取其路由及基础参数；
    4. 持久化写回运行期 catalog 覆盖文件并热重载 ApiRegistry。
    """
    raw_tree, live_pages = fetch_official_hydration_tree()
    if not live_pages:
        raise RuntimeError("官方目录树未返回任何接口，拒绝覆盖现有 catalog")

    # 读取运行期覆盖；损坏或空覆盖自动回退到随包静态 catalog。
    existing_catalog = _load_existing_catalog()

    catalog_by_doc_id: Dict[int, dict] = {
        item.get("doc_id"): item for item in existing_catalog if item.get("doc_id")
    }

    updated_count = 0
    new_count = 0
    final_catalog: List[dict] = []

    for lp in live_pages:
        doc_id = lp["doc_id"]
        title = lp["title"]
        path = lp["path"]
        doc_url = lp["doc_url"]

        category = path[0] if len(path) > 0 else "其它分类"
        subcategory = path[1] if len(path) > 1 else category

        if doc_id in catalog_by_doc_id:
            # 存在本地条目，更新路径与官方名称
            item = catalog_by_doc_id[doc_id]
            if item.get("path") != path or item.get("name") != title:
                item["name"] = title
                item["path"] = path
                item["category"] = category
                item["subcategory"] = subcategory
                item["doc_url"] = doc_url
                updated_count += 1
            final_catalog.append(item)
        else:
            # 官方新发布的接口！动态拉取其接口详情与 Command
            new_count += 1
            cmd = ""
            desc = ""
            detail = fetch_doc_detail_api(doc_id)
            if detail:
                desc = detail.get("description", "")
                keywords = detail.get("keywords", "")
                # 从 keywords 寻找 v4/xxx/yyy
                cmd_m = re.search(r"v4/([a-zA-Z0-9_]+/[a-zA-Z0-9_]+)", keywords)
                if cmd_m:
                    cmd = cmd_m.group(1)

            service_name = cmd.split("/")[0] if "/" in cmd else "openim"
            command = cmd.split("/")[1] if "/" in cmd else (cmd or f"cmd_{doc_id}")
            api_id = f"{service_name}_{command}" if cmd else f"tim_{doc_id}"

            new_item = {
                "api_id": api_id,
                "doc_id": doc_id,
                "name": title,
                "category": category,
                "subcategory": subcategory,
                "path": path,
                "service_name": service_name,
                "command": command,
                "full_command": f"v4/{cmd}" if cmd else f"v4/{service_name}/{command}",
                "doc_url": doc_url,
                "description": desc or f"腾讯云 IM {title} 官方接口",
                "fields": [],
                "sample_payload": {},
            }
            final_catalog.append(new_item)

    # 写回本地 catalog 文件
    atomic_write_json(CATALOG_PATH, final_catalog, mode=0o644)

    # 热重载本地注册表
    registry.reload()

    return {
        "status": "success",
        "total_apis": len(final_catalog),
        "new_apis": new_count,
        "updated_apis": updated_count,
        "sync_time": get_current_time_str(),
        "source": "window.__staticRouterHydrationData (https://cloud.tencent.com/document/product/269/1519)",
    }
