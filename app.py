"""
腾讯云 IM 可视化管理控制台 - FastAPI 服务
"""

import csv
import io
import json
import os
import zipfile
from typing import Any

import openpyxl
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from apis.agent import router as agent_router
from core.cloud_client import UniversalCloudClient
from core.common_specs_syncer import common_specs_manager
from core.config import (
    delete_environment,
    get_environments_payload,
    load_config,
    save_environment,
    switch_environment,
)
from core.doc_syncer import doc_sync_manager
from core.live_util import (
    generate_live_play_urls,
    generate_live_push_url,
)
from core.registry import registry
from core.task_runner import (
    clear_history,
    delete_history_item,
    load_history,
    save_history_item,
    task_runner,
)
from core.tree_syncer import fetch_official_hydration_tree, sync_catalog_from_live_tree

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(",")
    if origin.strip()
]

app = FastAPI(title="TC Studio (Nova) - Tencent Cloud Multi-Service Architecture Studio", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

app.include_router(agent_router)


# ---------------- 静态资源路由 ----------------
@app.get("/")
def serve_index():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(index_file):
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(index_file)


@app.get("/favicon.ico")
def serve_favicon():
    fav = os.path.join(STATIC_DIR, "favicon.svg")
    if os.path.exists(fav):
        return FileResponse(fav, media_type="image/svg+xml")
    raise HTTPException(status_code=404)


@app.get("/favicon.svg")
def serve_faviconsvg():
    fav = os.path.join(STATIC_DIR, "favicon.svg")
    if os.path.exists(fav):
        return FileResponse(fav, media_type="image/svg+xml")
    raise HTTPException(status_code=404)


# 挂载 Vite 构建输出静态资源目录
assets_dir = os.path.join(STATIC_DIR, "assets")
try:
    os.makedirs(assets_dir, exist_ok=True)
except OSError as exc:
    raise RuntimeError(f"无法创建静态资源目录: {assets_dir}") from exc
app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")


# ---------------- 多环境配置管理与鉴权 ----------------
class EnvironmentItem(BaseModel):
    id: str | None = None
    name: str = "未命名环境"
    tag: str | None = "DEV"
    remark: str | None = ""
    # 腾讯云 IM 专属配置
    sdkappid: int = 0
    secret_key: str | None = ""
    admin_user: str = "administrator"
    domain: str = "console.tim.qq.com"
    # 腾讯云 API 3.0 通用凭据 (云直播 / TRTC / VOD / COS 等通用)
    tc_secret_id: str | None = ""
    tc_secret_key: str | None = ""
    tc_region: str | None = "ap-guangzhou"
    tc_endpoint: str | None = "live.tencentcloudapi.com"
    # 云直播 (CSS) 专属配置 (推拉流域名与鉴权防盗链 Key)
    live_push_domain: str | None = ""
    live_play_domain: str | None = ""
    live_push_key: str | None = ""
    live_play_key: str | None = ""
    delay_sec: float = 0.05


class SwitchEnvRequest(BaseModel):
    env_id: str


class TestConnRequest(BaseModel):
    env_id: str | None = None
    service: str | None = None


@app.get("/api/environments")
def api_get_environments():
    return {"status": "success", "data": get_environments_payload(mask=True)}


@app.post("/api/environments/switch")
def api_switch_environment(req: SwitchEnvRequest):
    try:
        data = switch_environment(req.env_id)
        return {"status": "success", "data": data}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/environments/save")
def api_save_environment(req: EnvironmentItem):
    try:
        data = save_environment(req.dict())
        saved_env = data.get("saved_env") or {}
        return {"status": "success", "data": saved_env, "payload": data}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/environments/{env_id}")
def api_delete_environment(env_id: str):
    try:
        data = delete_environment(env_id)
        return {"status": "success", "data": data}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class ConfigUpdate(BaseModel):
    sdkappid: int
    secret_key: str
    admin_user: str = "administrator"
    domain: str = "console.tim.qq.com"
    delay_sec: float = 0.05


@app.get("/api/config")
def get_config():
    cfg = load_config()
    env_payload = get_environments_payload(mask=True)
    active_env = env_payload.get("active_env") or {}
    # Return only the masked active environment and masked environment list.
    # Never serialize the raw cfg because it contains nested cloud/live secrets.
    safe_data = {
        **active_env,
        "active_env_id": env_payload.get("active_env_id"),
        "environments": env_payload.get("environments", []),
        "has_secret": bool(cfg.get("secret_key")),
    }
    return {"status": "success", "data": safe_data}


@app.post("/api/config")
def update_config(body: ConfigUpdate):
    cfg = load_config()
    active_env_id = cfg.get("active_env_id", "env_default")
    env_data = {
        "id": active_env_id,
        "sdkappid": body.sdkappid,
        "secret_key": body.secret_key,
        "admin_user": body.admin_user,
        "domain": body.domain,
        "delay_sec": body.delay_sec,
    }
    saved_payload = save_environment(env_data)
    return {"status": "success", "data": saved_payload["active_env"]}


@app.post("/api/config/test")
def test_connectivity(req: TestConnRequest | None = None):
    cfg = load_config()
    env_id = req.env_id if req else None
    if env_id:
        target = next((e for e in cfg["environments"] if e.get("id") == env_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail=f"环境 ID {env_id} 不存在")
    else:
        target = cfg
    client = UniversalCloudClient(config=target)

    req_service = (req.service if req and req.service else "").lower()
    has_im = bool(target.get("sdkappid") and target.get("secret_key"))
    has_tc = bool(target.get("tc_secret_id") and target.get("tc_secret_key"))

    if not req_service:
        req_service = "live" if (has_tc and not has_im) else "im"

    try:
        res = client.test_connection(service=req_service)

        is_ok = (res.get("ActionStatus") == "OK" and res.get("ErrorCode") == 0)
        return {
            "status": "success" if is_ok else "fail",
            "is_ok": is_ok,
            "cost_ms": res.get("_cost_ms", 0),
            "service": req_service,
            "response": res
        }
    except Exception as e:
        return {"status": "error", "is_ok": False, "service": req_service, "message": str(e)}


# ---------------- 多服务与 API 注册工作台 ----------------
@app.get("/api/services")
def get_supported_services():
    """获取所有已支持的腾讯云产品服务列表 (IM、云直播等)"""
    return {"status": "success", "data": registry.list_services()}


@app.get("/api/apis")
def get_registered_apis(service: str | None = None):
    return {"status": "success", "data": registry.list_apis(service_type=service)}


class LiveSignUrlRequest(BaseModel):
    domain: str | None = None
    stream_name: str
    key: str | None = None
    expire_seconds: int | None = 86400
    app_name: str | None = "live"
    type: str | None = "both"  # "push", "play", "both"


@app.post("/api/live/sign-url")
def api_generate_live_url(req: LiveSignUrlRequest):
    """生成腾讯云直播推流与播放防盗链鉴权地址 (txSecret & txTime)"""
    cfg = load_config()
    push_domain = req.domain or cfg.get("live_push_domain") or "push.yourdomain.com"
    play_domain = req.domain or cfg.get("live_play_domain") or "play.yourdomain.com"
    push_key = req.key or cfg.get("live_push_key")
    play_key = req.key or cfg.get("live_play_key")

    push_info = generate_live_push_url(
        domain=push_domain,
        stream_name=req.stream_name,
        key=push_key,
        expire_seconds=req.expire_seconds or 86400,
        app_name=req.app_name or "live"
    )

    play_info = generate_live_play_urls(
        domain=play_domain,
        stream_name=req.stream_name,
        key=play_key,
        expire_seconds=req.expire_seconds or 86400,
        app_name=req.app_name or "live"
    )

    return {
        "status": "success",
        "data": {
            "stream_name": req.stream_name,
            "push": push_info,
            "play": play_info
        }
    }


class ApiInvokeRequest(BaseModel):
    payload: dict[str, Any]
    dry_run: bool | None = False


@app.post("/api/apis/{api_id}/invoke")
def invoke_single_api(api_id: str, req: ApiInvokeRequest):
    api_meta = registry.get_api(api_id)
    if not api_meta:
        raise HTTPException(status_code=404, detail=f"API {api_id} 未注册")

    client = UniversalCloudClient()
    payload = api_meta.build_payload(req.payload)

    # 预览模式：执行入参校验与请求报文结构生成，不发网络请求
    if req.dry_run:
        preview_data = client.generate_preview(api_meta, payload)
        return {
            "status": "success",
            "dry_run": True,
            "api_id": api_id,
            "service_name": api_meta.service_name,
            "service_type": getattr(api_meta, "service_type", "im"),
            "command": api_meta.command,
            "sent_payload": payload,
            "preview_url": preview_data.get("preview_url", ""),
            "curl_command": preview_data.get("curl_command", ""),
            "headers": preview_data.get("headers", {}),
            "response": {
                "ActionStatus": "OK",
                "ErrorCode": 0,
                "ErrorInfo": "",
                "_dry_run": True,
                "_message": "【预览模式】未向腾讯云发起真实网络请求。请求入参校验通过，报文结构与签名凭据已成功生成。"
            },
            "cost_ms": 1
        }

    resp = client.invoke_api(api_meta, payload)
    return {
        "status": "success",
        "dry_run": False,
        "api_id": api_id,
        "service_name": api_meta.service_name,
        "service_type": getattr(api_meta, "service_type", "im"),
        "command": api_meta.command,
        "sent_payload": payload,
        "response": resp,
        "cost_ms": resp.get("_cost_ms", 0)
    }


# ---------------- 官方结构化文档与异步批量同步 ----------------
class BatchSyncRequest(BaseModel):
    force: bool = False
    api_ids: list[str] | None = None
    mode: str | None = "auto"  # 'auto' | 'html' | 'api'
    service: str | None = None  # 'im' | 'live' | 'all'


@app.get("/api/docs/summary")
def get_docs_summary(service: str | None = Query(None)):
    apis_list = registry.list_apis()
    if service and service != "all":
        apis_list = [a for a in apis_list if (a.get("service_type") or "im") == service]
    summary = doc_sync_manager.get_summary(apis_list)
    return {"status": "success", "data": summary}


@app.get("/api/docs/sync/status")
def get_docs_sync_status():
    status = doc_sync_manager.get_status()
    return {"status": "success", "data": status}


@app.post("/api/docs/sync/batch")
def start_batch_docs_sync(req: BatchSyncRequest = BatchSyncRequest()):
    all_apis = registry.list_apis()
    if req.api_ids:
        target_set = set(req.api_ids)
        target_apis = [a for a in all_apis if a.get("api_id") in target_set]
    elif req.service and req.service != "all":
        target_apis = [a for a in all_apis if (a.get("service_type") or "im") == req.service]
    else:
        target_apis = all_apis

    res = doc_sync_manager.start_batch_sync(target_apis, force=req.force, mode=req.mode or "auto")
    return {"status": "success", "data": res}


@app.post("/api/docs/sync/cancel")
def cancel_batch_docs_sync():
    res = doc_sync_manager.cancel_batch_sync()
    return {"status": "success", "data": res}


@app.post("/api/docs/tree/sync")
def sync_official_tree():
    try:
        res = sync_catalog_from_live_tree()
        return {"status": "success", "data": res}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"同步官方目录树失败: {exc!s}") from exc


@app.post("/api/live/catalog/sync")
def sync_official_live_catalog():
    """动态同步腾讯云官方直播 (CSS) 260+ 全量 API"""
    try:
        from scripts.sync_live_catalog import sync_all_live_apis
        catalog = sync_all_live_apis()
        registry.reload()
        return {"status": "success", "count": len(catalog)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"同步云直播官方接口失败: {exc!s}") from exc


@app.get("/api/docs/tree/raw")
def get_raw_official_tree():
    snapshot_path = os.path.join(BASE_DIR, "storage", "official_live_tree.json")
    if os.path.exists(snapshot_path):
        try:
            with open(snapshot_path, encoding="utf-8") as f:
                return {"status": "success", "data": json.load(f)}
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=500, detail=f"读取官方目录树快照失败: {exc}") from exc
    try:
        raw_tree, _ = fetch_official_hydration_tree()
        return {"status": "success", "data": raw_tree}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取官方目录树快照失败: {exc!s}") from exc


@app.get("/api/docs/{api_id}")
def get_api_doc(api_id: str):
    api = registry.get_api(api_id)
    if not api:
        api = registry.get_api(f"tim_{api_id}")
    if not api:
        raise HTTPException(status_code=404, detail=f"API {api_id} 未找到")

    doc = doc_sync_manager.get_doc_for_api(api.to_metadata())
    return {"status": "success", "data": doc}


@app.post("/api/docs/sync/{api_id}")
def sync_single_api_doc(api_id: str, mode: str = Query("auto")):
    api = registry.get_api(api_id)
    if not api:
        api = registry.get_api(f"tim_{api_id}")
    if not api:
        raise HTTPException(status_code=404, detail=f"API {api_id} 未找到")

    meta = api.to_metadata()
    doc_id = meta.get("doc_id")
    if not doc_id:
        raise HTTPException(status_code=400, detail="该接口未配置官方文档 ID")

    try:
        doc = doc_sync_manager.sync_single(
            doc_id,
            api_id=meta.get("api_id", api_id),
            doc_url=meta.get("doc_url", ""),
            mode=mode
        )
        return {"status": "success", "data": doc}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"同步官方文档失败: {exc!s}") from exc


# ---------------- 腾讯云 API 3.0 公共规范与数据结构 ----------------
@app.get("/api/common-specs")
def get_common_specs(service: str = Query("live"), category: str | None = Query(None)):
    specs = common_specs_manager.get_specs(service=service)
    if category:
        if category in specs:
            return {"status": "success", "data": specs[category]}
        elif category in specs.get("invocation", {}):
            return {"status": "success", "data": specs["invocation"][category]}
        raise HTTPException(status_code=404, detail=f"Category '{category}' 未在公共规范中找到")
    return {"status": "success", "data": specs}


@app.get("/api/common-specs/error-lookup")
def lookup_common_error(code: str = Query(..., description="错误码，如 FailedOperation.StreamNotExist")):
    err = common_specs_manager.lookup_error(code)
    if not err:
        return {"status": "not_found", "message": f"未在官方错误码库中找到 {code}", "data": None}
    return {"status": "success", "data": err}


@app.post("/api/common-specs/sync")
def sync_common_specs(service: str = Query("live")):
    try:
        specs = common_specs_manager.sync_all(service=service)
        return {
            "status": "success",
            "message": "公共规范同步完成",
            "data": {
                "service": service,
                "updated_at": specs.get("updated_at"),
                "total_docs": specs.get("total_docs"),
                "structs_count": specs.get("structs", {}).get("total_count", 0),
                "errors_count": specs.get("errors", {}).get("total_count", 0),
                "countries_count": specs.get("mappings", {}).get("countries_count", 0),
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"同步公共规范失败: {exc!s}") from exc


MAX_BATCH_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_SPREADSHEET_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_SPREADSHEET_ROWS = 100_000
MAX_SPREADSHEET_COLUMNS = 200


def _validate_xlsx_archive(content: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            total_size = 0
            for info in archive.infolist():
                if info.file_size > MAX_SPREADSHEET_EXPANDED_BYTES:
                    raise ValueError("Excel 压缩成员解压后超过限制")
                total_size += info.file_size
                if total_size > MAX_SPREADSHEET_EXPANDED_BYTES:
                    raise ValueError("Excel 文件解压后总大小超过限制")
                if info.compress_size and info.file_size / info.compress_size > 200:
                    raise ValueError("检测到异常高压缩比，拒绝可能的压缩炸弹")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("Excel 压缩包结构无效") from exc

def _parse_batch_upload(filename: str, content: bytes) -> tuple[list[dict], str | None]:
    rows: list[dict] = []
    if filename.endswith(".xls"):
        return [], "不支持旧版 .xls 格式，请另存为 .xlsx 后重新上传"
    if filename.endswith(".xlsx"):
        try:
            _validate_xlsx_archive(content)
            wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
            sheet = wb.active
            if sheet is None:
                return [], "Excel 工作表为空"
            row_iter = sheet.iter_rows(values_only=True)
            header_row = next(row_iter, None)
            if not header_row:
                return [], "Excel 文件为空"
            header = [str(c or "").strip() for c in header_row[:MAX_SPREADSHEET_COLUMNS]]
            for row_index, row in enumerate(row_iter, start=1):
                if row_index > MAX_SPREADSHEET_ROWS:
                    return [], "Excel 行数超过限制"
                if any(row):
                    rows.append({
                        header[index]: str(value) if value is not None else ""
                        for index, value in enumerate(row[:MAX_SPREADSHEET_COLUMNS])
                        if index < len(header) and header[index]
                    })
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            return [], str(exc)
    elif filename.endswith(".csv"):
        text = content.decode("utf-8-sig", errors="ignore")
        reader = csv.DictReader(io.StringIO(text))
        for row_index, row in enumerate(reader, start=1):
            if row_index > MAX_SPREADSHEET_ROWS:
                return [], "CSV 行数超过限制"
            rows.append({k.strip(): str(v).strip() for k, v in row.items() if k})
    elif filename.endswith(".json"):
        try:
            data = json.loads(content.decode("utf-8", errors="ignore"))
        except json.JSONDecodeError as exc:
            return [], f"JSON 格式无效: {exc.msg}"
        if not isinstance(data, list):
            return [], "JSON 必须为对象数组格式"
        if len(data) > MAX_SPREADSHEET_ROWS:
            return [], "JSON 行数超过限制"
        rows = data
    else:
        return [], "仅支持 .xlsx, .csv, .json 格式文件"
    return rows, None


# ---------------- 批处理任务 ----------------
@app.post("/api/batch/upload")
async def upload_batch_file(file: UploadFile = File(...)):
    raw_filename = file.filename or ""
    filename = raw_filename.lower()
    if not filename:
        raise HTTPException(status_code=400, detail="上传文件名不能为空")
    raw_content = await file.read(10 * 1024 * 1024 + 1)
    if len(raw_content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="批处理文件超过 10 MiB 限制")
    rows, error_message = await run_in_threadpool(_parse_batch_upload, filename, raw_content)
    if error_message:
        return {"status": "fail", "message": error_message}

    return {
        "status": "success",
        "filename": raw_filename,
        "total_rows": len(rows),
        "columns": list(rows[0].keys()) if rows else [],
        "preview": rows[:10],
        "rows": rows
    }


@app.get("/api/batch/presets")
def get_batch_presets(service: str | None = None):
    """获取常用运维场景预设清单，支持按 service 过滤"""
    from core.batch_presets import BATCH_PRESETS
    if service:
        filtered = [p for p in BATCH_PRESETS if p.get("service_type", "im") == service]
        return {"status": "success", "data": filtered}
    return {"status": "success", "data": BATCH_PRESETS}


@app.get("/api/batch/presets/{preset_id}/csv")
def download_preset_csv(preset_id: str):
    """直接下载该预设的标准 CSV 模板（含 UTF-8 BOM，Excel 双击无乱码）"""
    from core.batch_presets import BATCH_PRESETS
    preset = next((p for p in BATCH_PRESETS if p["id"] == preset_id), None)
    if not preset:
        raise HTTPException(status_code=404, detail="预设模版未找到")

    output = io.StringIO()
    output.write('\ufeff')
    writer = csv.DictWriter(output, fieldnames=preset["columns"])
    writer.writeheader()
    for row in preset["sample_rows"]:
        writer.writerow({col: row.get(col, "") for col in preset["columns"]})

    csv_data = output.getvalue()
    return Response(
        content=csv_data.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{preset["filename"]}"'}
    )


@app.get("/api/batch/guidance/{api_id}")
def get_batch_guidance_api(api_id: str):
    """获取指定 API 的批量调用安全建议、QPS 限速规则与风控提示"""
    from core.batch_presets import get_api_batch_guidance
    data = get_api_batch_guidance(api_id)
    return {"status": "success", "data": data}


class CreateBatchRequest(BaseModel):
    api_id: str
    items: list[dict]
    delay_sec: float | None = 0.05
    dry_run: bool | None = False


@app.post("/api/batch/create")
def create_batch_task(req: CreateBatchRequest):
    if not req.items:
        raise HTTPException(status_code=400, detail="任务列表不能为空")
    delay_sec = req.delay_sec if req.delay_sec is not None else 0.05
    try:
        task = task_runner.create_task(req.api_id, req.items, delay_sec=delay_sec, dry_run=bool(req.dry_run))
        if not task_runner.start_task(task.task_id):
            raise HTTPException(status_code=409, detail="批处理任务已启动或已终止")
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {
        "status": "success",
        "task_id": task.task_id,
        "total": task.total,
        "api_id": task.api_id,
        "dry_run": req.dry_run or False
    }


@app.get("/api/batch/{task_id}/events")
def stream_batch_events(task_id: str):
    task = task_runner.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    sub_queue, history_events = task.register_subscriber()

    def event_generator():
        try:
            # 1. 先重放历史已发生事件，保证重连与刷新绝不丢失事件
            has_finished = False
            for evt in history_events:
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                if evt.get("type") in ("complete", "error"):
                    has_finished = True
                    break
            if has_finished or task.status in ("COMPLETED", "CANCELLED", "FAILED"):
                return

            # 2. 持续等待新事件
            while True:
                try:
                    evt = sub_queue.get(timeout=1.0)
                    yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                    if evt.get("type") in ("complete", "error") or task.status in ("COMPLETED", "CANCELLED", "FAILED"):
                        break
                except Exception:
                    if task.status in ("COMPLETED", "CANCELLED", "FAILED"):
                        break
                    yield ": keep-alive\n\n"
        finally:
            task.unregister_subscriber(sub_queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@app.post("/api/batch/{task_id}/pause")
def pause_task(task_id: str):
    task = task_runner.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task.pause()
    return {"status": "success", "task_status": task.status}


@app.post("/api/batch/{task_id}/resume")
def resume_task(task_id: str):
    task = task_runner.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task.resume()
    return {"status": "success", "task_status": task.status}


@app.post("/api/batch/{task_id}/stop")
def stop_task(task_id: str):
    task = task_runner.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task.stop()
    return {"status": "success", "task_status": task.status}


@app.get("/api/batch/{task_id}/export/csv")
def export_csv(task_id: str, only_failed: bool = False):
    task = task_runner.get_task(task_id)
    if not task or not task.results:
        # 尝试从 history 中查询
        history = load_history()
        match = next((h for h in history if h.get("task_id") == task_id), None)
        if match and match.get("results"):
            results = match["results"]
        else:
            raise HTTPException(status_code=404, detail="Task results not found")
    else:
        results = task.results

    if only_failed:
        results = [r for r in results if r.get("_status") != "OK" or r.get("_err_code") != 0]

    output = io.StringIO()
    if results:
        fieldnames = list(dict.fromkeys(k for r in results for k in r))
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    output.seek(0)
    filename_suffix = "failed_items" if only_failed else "results"
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=batch_{task_id}_{filename_suffix}.csv"}
    )


# ---------------- 历史归档 ----------------
@app.get("/api/history")
def get_history_list():
    history = load_history()
    # 如果 history 为空但存在 withdraw_results.json，自动生成一条初始历史记录
    if not history:
        prev_res_path = os.path.join(BASE_DIR, "withdraw_results.json")
        if os.path.exists(prev_res_path):
            try:
                with open(prev_res_path, encoding="utf-8") as f:
                    data = json.load(f)
                    init_record = {
                        "task_id": "init_208",
                        "api_id": "c2c_msg_withdraw",
                        "api_name": "撤回单聊消息 (历史归档)",
                        "total": len(data),
                        "success": sum(1 for d in data if d.get("status") == "OK" and d.get("err_code") == 0),
                        "failed": sum(1 for d in data if not (d.get("status") == "OK" and d.get("err_code") == 0)),
                        "duration_sec": 53.28,
                        "status": "COMPLETED",
                        "created_at": "2026-10-23 15:18:00",
                        "results": data
                    }
                    save_history_item(init_record)
                    history = [init_record]
            except (OSError, json.JSONDecodeError):
                history = load_history()

    # 列表仅返回摘要，不带 results 大数据
    summary_list = []
    for h in history:
        item = dict(h)
        if "results" in item:
            item["result_count"] = len(item["results"])
            del item["results"]
        summary_list.append(item)
    return {"status": "success", "data": summary_list}


@app.get("/api/history/{task_id}")
def get_history_detail(task_id: str):
    history = load_history()
    match = next((h for h in history if h.get("task_id") == task_id), None)
    if not match:
        raise HTTPException(status_code=404, detail="History record not found")
    return {"status": "success", "data": match}


@app.delete("/api/history/{task_id}")
def delete_single_history(task_id: str):
    success = delete_history_item(task_id)
    if not success:
        raise HTTPException(status_code=404, detail="History record not found")
    return {"status": "success", "message": f"Task {task_id} deleted"}


@app.delete("/api/history")
def delete_all_history():
    clear_history()
    return {"status": "success", "message": "All history records cleared"}


if __name__ == "__main__":
    import uvicorn
    print("\n" + "=" * 60)
    print("🚀 TC Studio (Nova) - 腾讯云多业务云架构控制台启动中...")
    print("👉 请在浏览器打开: http://127.0.0.1:8000")
    print("=" * 60 + "\n")
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
