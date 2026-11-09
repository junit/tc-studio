# -*- coding: utf-8 -*-
"""
全面排查与同步云直播 (CSS) 261 个 API 官方文档
检查项：
1. 抓取完整性（是否有拉取失败）
2. 标题与功能概述（Overview）解析质量
3. 输入参数表格（Request Body Fields）解析完整度与公共参数标记
4. 输出参数表格（Response Body Fields）解析完整度
5. 请求示例（Request Examples）是否提取了纯净业务参数（杜绝回包混入）
6. 应答示例（Response Examples）是否正确归类与 JSON 化
7. 错误码列表（Error Codes）解析
"""

import os
import sys
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.doc_syncer import parse_official_doc, save_doc, get_doc_by_id
from core.runtime_paths import catalog_read_path, package_catalog_path
from core.tree_syncer import fetch_doc_detail_api


def _load_live_catalog() -> list[dict]:
    """Load a usable runtime catalog, falling back to the package copy."""

    candidates = (catalog_read_path("live"), package_catalog_path("live"))
    seen: set[str] = set()
    for candidate in candidates:
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
            print(f"[WARN] 忽略损坏或空的云直播 catalog: {candidate}")
        except (OSError, ValueError) as exc:
            print(f"[WARN] 读取云直播 catalog 失败 {candidate}: {exc}")
    raise RuntimeError("没有可用的云直播 catalog，请先完成目录同步或检查发布包")


def run_audit_and_sync():
    live_apis = _load_live_catalog()

    total = len(live_apis)
    print(f"============================================================")
    print(f"🔍 开始全量排查并同步云直播 {total} 个 API 文档...")
    print(f"============================================================")

    start_time = time.time()
    results = []

    def task(api):
        doc_id = api["doc_id"]
        api_id = api["api_id"]
        doc_url = api.get("doc_url", "")
        name = api.get("name", "")
        time.sleep(0.08)  # Gentle pacing to respect CDN edge limits
        try:
            doc = parse_official_doc(doc_id, api_id=api_id, doc_url=doc_url, mode="auto")
            save_doc(doc)
            return {"status": "ok", "api_id": api_id, "doc_id": doc_id, "name": name, "doc": doc}
        except Exception as e:
            return {"status": "error", "api_id": api_id, "doc_id": doc_id, "name": name, "error": str(e)}

    # Concurrently sync all 261 docs with mild concurrency to avoid CDN 567 rate-limits
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(task, a): a for a in live_apis}
        done = 0
        errors = 0
        for fut in as_completed(futures):
            res = fut.result()
            results.append(res)
            done += 1
            if res["status"] == "error":
                errors += 1
                print(f"  ❌ [{done}/{total}] doc_id={res['doc_id']} ({res['name']}) 失败: {res['error']}")
            elif done % 40 == 0 or done == total:
                print(f"  ⚡ 进度: [{done}/{total}] 已完成 (异常数: {errors})")

    elapsed = time.time() - start_time
    print(f"\n✅ 抓取与同步完成，耗时 {elapsed:.2f} 秒。开始深度结构审计...\n")

    # ==================== 审计指标统计 ====================
    fetch_success = sum(1 for r in results if r["status"] == "ok")
    fetch_failed = sum(1 for r in results if r["status"] == "error")

    anomalies = {
        "empty_title": [],
        "empty_overview": [],
        "no_req_fields": [],
        "no_resp_fields": [],
        "resp_in_req_examples": [],
        "empty_resp_examples": [],
        "invalid_json_req": [],
        "invalid_json_resp": [],
        "no_error_codes": [],
    }

    req_fields_counts = []
    resp_fields_counts = []

    for r in results:
        if r["status"] != "ok":
            continue
        doc = r["doc"]
        did = r["doc_id"]
        name = r["name"]

        # 1. 标题
        if not doc.get("title"):
            anomalies["empty_title"].append((did, name))

        # 2. 描述
        if not doc.get("overview"):
            anomalies["empty_overview"].append((did, name))

        # 3. 入参表格
        req_f = doc.get("request_body_fields", [])
        req_fields_counts.append(len(req_f))
        if len(req_f) == 0:
            anomalies["no_req_fields"].append((did, name))

        # 4. 出参表格
        resp_f = doc.get("response_body_fields", [])
        resp_fields_counts.append(len(resp_f))
        if len(resp_f) == 0:
            anomalies["no_resp_fields"].append((did, name))

        # 5. 请求示例检查（检查是否含有未转换的回包 Response/RequestId）
        req_ex = doc.get("request_examples", [])
        for ex in req_ex:
            c = ex.get("content", "")
            cj = ex.get("clean_json", "")
            if '"Response":' in c or "'Response':" in c:
                anomalies["resp_in_req_examples"].append((did, name, ex.get("title")))
            if cj:
                try:
                    json.loads(cj)
                except Exception:
                    anomalies["invalid_json_req"].append((did, name, ex.get("title"), cj[:60]))

        # 6. 回包示例检查
        resp_ex = doc.get("response_examples", [])
        if len(resp_ex) == 0:
            anomalies["empty_resp_examples"].append((did, name))
        for ex in resp_ex:
            cj = ex.get("clean_json", "")
            if cj:
                try:
                    json.loads(cj)
                except Exception:
                    anomalies["invalid_json_resp"].append((did, name, ex.get("title"), cj[:60]))

        # 7. 错误码
        err_c = doc.get("error_codes", [])
        if len(err_c) == 0:
            anomalies["no_error_codes"].append((did, name))

    print("================ 审计报告 ================")
    print(f"📊 接口总数: {total}")
    print(f"✅ 同步成功: {fetch_success} ({fetch_success/total*100:.1f}%)")
    print(f"❌ 同步失败: {fetch_failed}")
    print(f"📑 平均请求参数数: {sum(req_fields_counts)/len(req_fields_counts):.1f} 项/接口")
    print(f"📑 平均响应参数数: {sum(resp_fields_counts)/len(resp_fields_counts):.1f} 项/接口")
    print("------------------------------------------")
    print(f"⚠️ 标题为空: {len(anomalies['empty_title'])}")
    print(f"⚠️ 概述为空: {len(anomalies['empty_overview'])}")
    print(f"⚠️ 入参表格为空: {len(anomalies['no_req_fields'])}")
    if anomalies["no_req_fields"]:
        print("   -> 样例:", anomalies["no_req_fields"][:5])
    print(f"⚠️ 出参表格为空: {len(anomalies['no_resp_fields'])}")
    if anomalies["no_resp_fields"]:
        print("   -> 样例:", anomalies["no_resp_fields"][:5])
    print(f"🚨 请求示例含有回包 Response: {len(anomalies['resp_in_req_examples'])}")
    if anomalies["resp_in_req_examples"]:
        print("   -> 样例:", anomalies["resp_in_req_examples"][:5])
    print(f"⚠️ 缺少响应示例: {len(anomalies['empty_resp_examples'])}")
    if anomalies["empty_resp_examples"]:
        print("   -> 样例:", anomalies["empty_resp_examples"][:5])
    print(f"🚨 请求示例 JSON 语法异常: {len(anomalies['invalid_json_req'])}")
    if anomalies["invalid_json_req"]:
        print("   -> 样例:", anomalies["invalid_json_req"][:5])
    print(f"🚨 响应示例 JSON 语法异常: {len(anomalies['invalid_json_resp'])}")
    if anomalies["invalid_json_resp"]:
        print("   -> 样例:", anomalies["invalid_json_resp"][:5])
    print(f"ℹ️ 无独立错误码表: {len(anomalies['no_error_codes'])}")
    print("==========================================")

    return anomalies


if __name__ == "__main__":
    run_audit_and_sync()
