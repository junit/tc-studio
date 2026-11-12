"""Trusted file encoders and explicit, requester-scoped artifact publication."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import mimetypes
import math
import os
import re
import tempfile
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from .file_parser import format_file_size, get_file_category_and_type, validate_zip_archive
from .pdf_reports import validate_pdf

MAX_TABLE_ROWS = 20_000
MAX_TABLE_COLUMNS = 200
MAX_TABLE_CELLS = 500_000
OFFICE_PARTS = {".xlsx": "xl/workbook.xml", ".docx": "word/document.xml", ".pptx": "ppt/presentation.xml"}


def safe_generated_name(filename: str, *, max_bytes: int = 180) -> str:
    if not isinstance(filename, str):
        raise ValueError("文件名必须是字符串")
    name = os.path.basename(filename)
    if not name or name in {".", ".."} or name.startswith(".") or any(ord(c) < 32 for c in name):
        raise ValueError("生成文件需要有效的非隐藏文件名")
    # Leave room for the immutable storage ID on filesystems with 255-byte names.
    if len(name.encode("utf-8")) > max_bytes:
        raise ValueError(f"文件名超过 {max_bytes} UTF-8 字节限制，请缩短名称")
    return name


def _table(data: Any) -> list[list[Any]]:
    if not isinstance(data, list) or len(data) > MAX_TABLE_ROWS:
        raise ValueError("表格需要列表，最多 20000 行")
    if not data:
        return []
    if all(isinstance(row, dict) for row in data):
        headers = list(dict.fromkeys(key for row in data for key in row))
        if not all(isinstance(key, str) for key in headers):
            raise ValueError("表格列名必须是字符串")
        rows = [headers] + [[row.get(key) for key in headers] for row in data]
    elif all(isinstance(row, list) for row in data):
        rows = data
    else:
        raise ValueError("表格需要对象列表或二维列表")
    if any(len(row) > MAX_TABLE_COLUMNS for row in rows) or sum(map(len, rows)) > MAX_TABLE_CELLS:
        raise ValueError("表格超过 200 列或 500000 单元格限制")
    return rows


def _cell(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("表格不支持 NaN 或 Infinity")
    if isinstance(value, str) and len(value) > 32767:
        raise ValueError("单元格内容超过 32767 字符限制")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _csv_cell(value: Any) -> Any:
    value = _cell(value)
    if isinstance(value, str) and (value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@"))):
        return "'" + value
    return value


def encode_generated_file(filename: str, data: Any, *, strict: bool = True) -> bytes:
    """Encode common formats, rather than saving JSON with an Office suffix."""
    ext = os.path.splitext(filename)[1].lower()
    if strict and ext == ".xls":
        raise ValueError("不支持生成旧版 .xls，请使用 .xlsx")
    if strict and ext == ".bin":
        raise ValueError("BIN 探测文件只能保存在临时工作区，不能作为正式产物发布")
    if ext == ".xlsx" and not isinstance(data, (bytes, bytearray)):
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        for row in _table(data):
            sheet.append([_cell(value) for value in row])
            # External text is always text, including strings beginning with '='.
            for cell in sheet[sheet.max_row]:
                if isinstance(cell.value, str):
                    cell.data_type = "s"
        output = io.BytesIO()
        workbook.save(output)
        workbook.close()
        return output.getvalue()
    if ext == ".docx" and isinstance(data, str):
        from docx import Document

        if len(data) > 200_000:
            raise ValueError("DOCX 正文超过 200000 字符限制")
        document = Document()
        for line in data.splitlines():
            heading = re.match(r"^(#{1,6})\s+(.+)", line)
            if heading:
                document.add_heading(heading[2], level=len(heading[1]))
            else:
                document.add_paragraph(line)
        output = io.BytesIO()
        document.save(output)
        return output.getvalue()
    if ext == ".csv":
        if isinstance(data, (str, bytes, bytearray)):
            text = data if isinstance(data, str) else bytes(data).decode("utf-8-sig")
            rows = []
            for row in csv.reader(io.StringIO(text)):
                rows.append(row)
                if len(rows) > MAX_TABLE_ROWS:
                    raise ValueError("CSV 超过 20000 行限制")
            rows = _table(rows)
        else:
            rows = _table(data)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerows([_csv_cell(value) for value in row] for row in rows)
        return output.getvalue().encode("utf-8-sig")
    if ext == ".json":
        if isinstance(data, (str, bytes, bytearray)):
            data = json.loads(data)
        return json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if strict and ext in {".pdf", ".docx", ".pptx", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        raise ValueError(f"{ext} 需要真实格式的二进制内容，不能把文字或 JSON 改后缀；PDF 请使用 create_pdf_report")
    if isinstance(data, (dict, list)):
        return json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    if not isinstance(data, str):
        raise ValueError("文件内容必须是文本、字节或 JSON 数据")
    return data.encode("utf-8")


def validate_generated_file(path: str) -> None:
    ext = os.path.splitext(path)[1].lower()
    if not os.path.getsize(path):
        raise ValueError("不能发布空文件")
    if ext == ".pdf":
        validate_pdf(path)
    elif ext in OFFICE_PARTS or ext == ".zip":
        import zipfile

        try:
            validate_zip_archive(path)
            with zipfile.ZipFile(path) as archive:
                if ext in OFFICE_PARTS and (OFFICE_PARTS[ext] not in archive.namelist() or "[Content_Types].xml" not in archive.namelist()):
                    raise ValueError("缺少 Office 文档主体")
                if archive.testzip() is not None:
                    raise ValueError("压缩文件内容校验失败")
            if ext == ".xlsx":
                from openpyxl import load_workbook

                workbook = load_workbook(path, read_only=True, keep_links=False)
                workbook.close()
            elif ext == ".docx":
                from docx import Document

                Document(path)
        except Exception as exc:
            raise ValueError(f"无效的 {ext} 文件: {exc}") from exc
    elif ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        from PIL import Image

        with Image.open(path) as image:
            expected = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".gif": "GIF", ".webp": "WEBP"}[ext]
            if image.format != expected or image.width * image.height > 40_000_000:
                raise ValueError("图片格式或像素数不符合要求")
            image.verify()
    elif ext == ".json":
        with open(path, encoding="utf-8-sig") as stream:
            json.load(stream)


class GeneratedFileSession:
    """Keep scratch files separate from immutable, indexed user deliverables."""

    def __init__(self, manager, bot_id: str, writer, confined_open):
        self.manager = manager
        self.bot_id = bot_id
        self.writer = writer
        self.confined_open = confined_open
        self.uploads_dir = manager._uploads_dir(bot_id)
        os.makedirs(self.uploads_dir, mode=0o700, exist_ok=True)
        self._workspace = tempfile.TemporaryDirectory(prefix=".work-", dir=self.uploads_dir)
        self.work_dir = self._workspace.name
        self.files: list[dict[str, Any]] = []

    def save_file(self, filename: str, data: Any, publish: bool = False) -> dict[str, Any]:
        name = safe_generated_name(filename)
        if not isinstance(publish, bool):
            raise ValueError("publish 必须是布尔值")
        if not publish:
            path = self.writer(self.work_dir, name, data, self.manager._lock, unique=False, validate=False)
            return {"filename": name, "path": path, "size": os.path.getsize(path), "published": False,
                    "message": "已保存到临时工作区；正式交付请调用 publish_file(filename)，会话结束后临时文件会删除"}
        scope = self.manager._metadata_scope(self.bot_id)
        file_id = uuid.uuid4().hex
        path = None
        # This lock is shared by API processes on the same host. The SQL slot
        # reservation also counts in-progress uploads before writing any bytes.
        with self.manager._history_storage_lock(self.bot_id):
            if not self.manager.metadata_store.reserve_file_slot(reservation_id=file_id, bot_id=self.bot_id, max_files=1000, **scope):
                raise ValueError("该智能体文件数量已达到上限，请先清理旧文件")
            try:
                path = self.writer(self.uploads_dir, name, data, self.manager._lock)
                digest = hashlib.sha256()
                with open(path, "rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                stored_name = os.path.basename(path)
                size = os.path.getsize(path)
                self.manager.register_bot_file(self.bot_id, file_id=file_id, original_name=name,
                    stored_name=stored_name, content_type=mimetypes.guess_type(name)[0], size_bytes=size, sha256=digest.hexdigest())
            except Exception:
                if path is not None:
                    with suppress(OSError):
                        os.remove(path)
                self.manager.metadata_store.release_file_slot(file_id, **scope)
                raise
        category, file_type = get_file_category_and_type(name)
        url = f"/api/agent/bots/{self.bot_id}/files/{quote(stored_name, safe='')}"
        result = {"id": file_id, "filename": name, "name": name, "saved_name": stored_name,
            "size": size, "size_formatted": format_file_size(size), "path": path,
            "category": category, "type": file_type, "url": url, "download_url": url + "?download=true",
            "created_at": datetime.now(UTC).isoformat(), "published": True,
            "message": f"文件「{name}」已发布到当前用户的文件中心；请使用返回的 download_url"}
        self.files.append(result)
        return dict(result)

    def publish_file(self, filename: str) -> dict[str, Any]:
        name = safe_generated_name(filename)
        path = os.path.join(self.work_dir, name)
        if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) > 100 * 1024 * 1024:
            raise ValueError("只能发布当前临时工作区内不超过 100 MiB 的普通文件")
        with open(path, "rb") as stream:
            return self.save_file(name, stream.read(), publish=True)

    def open(self, file, mode="r", *args, **kwargs):
        name = safe_generated_name(os.fsdecode(os.fspath(file)), max_bytes=255)
        if any(flag in mode for flag in "wax+"):
            if any(name == item["saved_name"] for item in self.files):
                raise PermissionError("已发布文件只读；请用新文件名生成并发布新版本")
            return self.confined_open(self.work_dir, name, mode, *args, **kwargs)
        root = self.work_dir if os.path.isfile(os.path.join(self.work_dir, name)) else self.uploads_dir
        return self.confined_open(root, name, mode, *args, **kwargs)

    def close(self):
        self._workspace.cleanup()
