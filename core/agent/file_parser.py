# ruff: noqa
# -*- coding: utf-8 -*-
"""
智能体文件与附件解析器 (FileParser)
支持 DOCX, XLSX/XLS, CSV, TXT, JSON, MD, LOG, PDF, 以及图片等多格式文档的自动解析、元数据提取与结构化摘要
"""

import base64
import csv
import io
import json
import os
import re
import shutil
import subprocess
import zipfile
from typing import Any, Dict, List, Optional, Tuple

MAX_OFFICE_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_SPREADSHEET_EXPANDED_BYTES = MAX_OFFICE_EXPANDED_BYTES
MAX_OFFICE_ZIP_MEMBERS = 10_000
# Compression ratios on tiny XML members are common in legitimate Office files
# (for example, repeated whitespace/style records).  Ratio checks only add
# useful bomb signal once a member is large enough to create material memory
# pressure; absolute member and archive limits remain enforced for all sizes.
MIN_ZIP_RATIO_CHECK_BYTES = 512 * 1024


def validate_zip_archive(
    content_or_path: bytes | bytearray | str | os.PathLike[str],
    max_expanded_bytes: int = MAX_OFFICE_EXPANDED_BYTES,
    max_ratio: float = 200.0,
    max_members: int = MAX_OFFICE_ZIP_MEMBERS,
) -> None:
    """Validate an Office ZIP before a parser expands its members.

    Both XLSX and DOCX are ZIP containers.  Inspecting the central directory
    first prevents openpyxl/python-docx from allocating unbounded expanded
    member data for a compression bomb.  The same helper accepts upload bytes
    and filesystem paths because the two ingestion paths use both forms.
    """
    if isinstance(content_or_path, (bytes, bytearray)):
        source = io.BytesIO(bytes(content_or_path))
    else:
        source = content_or_path
    try:
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            if len(members) > max_members:
                raise ValueError("Office 压缩包成员数量超过安全上限 (10000)")
            total_size = 0
            for info in members:
                if info.file_size > max_expanded_bytes:
                    raise ValueError("Office 压缩成员解压后超过安全上限 (64MB)")
                total_size += info.file_size
                if total_size > max_expanded_bytes:
                    raise ValueError("Office 压缩包总解压容量超过安全上限 (64MB)")
                if (
                    info.compress_size
                    and info.file_size > MIN_ZIP_RATIO_CHECK_BYTES
                    and info.file_size / info.compress_size > max_ratio
                ):
                    raise ValueError("检测到异常高压缩比，拒绝可能的压缩炸弹 (Zip Bomb)")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("Office 压缩文件结构无效或损坏") from exc


# Kept as a compatibility alias for callers that specifically parse XLSX.
validate_xlsx_archive = validate_zip_archive


_SWIFT_OCR_SCRIPT = """
import AppKit
import Vision
import Foundation

let path = CommandLine.arguments[1]
guard let image = NSImage(contentsOfFile: path) else { exit(0) }
var rect = CGRect(origin: .zero, size: image.size)
guard let cgImage = image.cgImage(forProposedRect: &rect, context: nil, hints: nil) else { exit(0) }

let request = VNRecognizeTextRequest { req, err in
    guard let observations = req.results as? [VNRecognizedTextObservation] else { return }
    let strings = observations.compactMap { $0.topCandidates(1).first?.string }
    print(strings.joined(separator: "\\n"))
}
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.recognitionLevel = .accurate
let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
try? handler.perform([request])
"""


def recognize_image_text(file_path: str, max_chars: int = 3000) -> Optional[str]:
    """
    轻量级原生 OCR 文字识别：
    优先调用 macOS 原生 Apple Vision 框架 (VNRecognizeTextRequest，毫秒级响应，完全离线高精度)，
    无第三方环境时优雅降级。
    """
    if not os.path.exists(file_path):
        return None

    # 1. 尝试 macOS 原生 Apple Vision OCR (通过 swift - 极速进程内调用，无须 pip 安装庞大机器学习框架)
    swift_bin = shutil.which("swift")
    if swift_bin and os.name != "nt":
        try:
            proc = subprocess.run(
                [swift_bin, "-", os.path.abspath(file_path)],
                input=_SWIFT_OCR_SCRIPT,
                capture_output=True,
                text=True,
                timeout=4.0
            )
            if proc.returncode == 0:
                output = proc.stdout.strip()
                if output:
                    if len(output) > max_chars:
                        output = output[:max_chars] + f"\n... [OCR 内容过长，已截取前 {max_chars} 字符]"
                    return output
        except Exception:
            pass

    # 2. 尝试 pytesseract (如果宿主已安装)
    try:
        import pytesseract  # type: ignore[import-not-found]
        from PIL import Image
        # Keep the descriptor-backed image resource scoped to this OCR call.
        # Pillow may defer closing the underlying file until image GC otherwise,
        # which is particularly visible during high-volume fallback OCR.
        with Image.open(file_path) as img:
            txt = pytesseract.image_to_string(img, lang="chi_sim+eng").strip()
        if txt:
            return txt[:max_chars]
    except Exception:
        pass

    return None



def get_file_category_and_type(filename: str) -> Tuple[str, str]:
    """根据文件名后缀获取类别与标准化格式标识"""
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    if ext in ("xlsx", "xls"):
        return "table", "xlsx"
    elif ext == "csv":
        return "table", "csv"
    elif ext in ("docx", "doc"):
        return "document", "docx"
    elif ext == "pdf":
        return "document", "pdf"
    elif ext in ("png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"):
        return "image", ext
    elif ext in ("txt", "log", "ini", "conf"):
        return "text", "txt"
    elif ext in ("json", "jsonl"):
        return "code", "json"
    elif ext in ("md", "markdown"):
        return "document", "markdown"
    elif ext in ("py", "js", "ts", "html", "css", "sql", "sh"):
        return "code", ext
    return "other", ext or "file"


def format_file_size(size_bytes: int) -> str:
    """人类可读的文件大小格式化"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def read_text_with_encoding(file_path: str, max_chars: int = 5000) -> str:
    """尝试以 UTF-8、GBK、GB18030、latin-1 等多重编码鲁棒读取文本"""
    encodings = ["utf-8", "gb18030", "gbk", "utf-16", "latin-1"]
    for enc in encodings:
        try:
            with open(file_path, "r", encoding=enc, errors="replace") as f:
                content = f.read(max_chars)
                return content
        except Exception:
            continue
    return ""


def parse_file_summary(file_path: str, filename: str) -> Dict[str, Any]:
    """
    全量解析文件内容，提取面向大模型上下文与前端展示的高密度摘要
    """
    if not os.path.exists(file_path):
        return {
            "error": "文件不存在",
            "summary": "文件未找到",
            "type": "unknown",
            "category": "other"
        }

    size = os.path.getsize(file_path)
    category, ftype = get_file_category_and_type(filename)
    result: Dict[str, Any] = {
        "filename": filename,
        "size": size,
        "size_formatted": format_file_size(size),
        "category": category,
        "type": ftype,
        "summary": "",
        "preview": "",
        "meta": {}
    }

    try:
        # 1. Excel 表格 (.xlsx, .xls)
        if ftype == "xlsx":
            try:
                if os.path.splitext(filename)[1].lower() == ".xls":
                    raise ValueError("不支持旧版 .xls 格式，请另存为 .xlsx 后重新上传")
                validate_xlsx_archive(file_path)
                import openpyxl
                wb = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
                sheet_names = wb.sheetnames
                active_sheet = wb.active or wb[sheet_names[0]]
                sample_rows = []
                row_count = 0
                for r in active_sheet.iter_rows(values_only=True):
                    row_count += 1
                    if row_count <= 6:
                        # 转换每行为字符串，避免序列化问题
                        sample_rows.append([str(v) if v is not None else "" for v in r[:10]])

                wb.close()
                headers = sample_rows[0] if sample_rows else []
                data_preview = sample_rows[1:] if len(sample_rows) > 1 else []

                meta = {
                    "sheets": sheet_names,
                    "active_sheet": active_sheet.title if active_sheet else sheet_names[0],
                    "sample_headers": headers,
                    "preview_rows_count": len(data_preview)
                }
                result["meta"] = meta

                summary_lines = [
                    f"Excel 工作簿（包含 {len(sheet_names)} 个 Sheet: {', '.join(sheet_names)}）",
                    f"首选工作表「{meta['active_sheet']}」前 {len(headers)} 列: {headers}",
                ]
                if data_preview:
                    summary_lines.append("数据前几行样本:")
                    for row in data_preview[:3]:
                        summary_lines.append(f"  - {row}")
                result["summary"] = "\n".join(summary_lines)
                result["preview"] = result["summary"]
            except Exception as e:
                result["summary"] = f"Excel 表格 ({format_file_size(size)})，解析轻微受限: {str(e)[:100]}"
                result["preview"] = result["summary"]

        # 2. CSV 表格 (.csv)
        elif ftype == "csv":
            try:
                text_sample = read_text_with_encoding(file_path, max_chars=10000)
                reader = csv.reader(io.StringIO(text_sample))
                rows = [row for row in reader if row]
                headers = rows[0] if rows else []
                sample_data = rows[1:5] if len(rows) > 1 else []
                result["meta"] = {
                    "columns": headers,
                    "sample_rows_count": len(sample_data)
                }
                summary_lines = [
                    f"CSV 表格文件（共约 {len(rows)} 行采样数据）",
                    f"表头字段: {headers}",
                ]
                if sample_data:
                    summary_lines.append("前几行数据样本:")
                    for row in sample_data[:3]:
                        summary_lines.append(f"  - {row}")
                result["summary"] = "\n".join(summary_lines)
                result["preview"] = result["summary"]
            except Exception as e:
                result["summary"] = f"CSV 表格文件 ({format_file_size(size)})"
                result["preview"] = result["summary"]

        # 3. Word 文档 (.docx)
        elif ftype == "docx":
            try:
                validate_zip_archive(file_path)
                import docx
                doc = docx.Document(file_path)
                paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
                tables_count = len(doc.tables)
                text_sample = "\n".join(paragraphs[:8])
                if len(text_sample) > 800:
                    text_sample = text_sample[:800] + "... [后续内容已截断]"

                result["meta"] = {
                    "paragraph_count": len(paragraphs),
                    "table_count": tables_count
                }
                result["summary"] = (
                    f"Word 文档（包含约 {len(paragraphs)} 个有效段落，{tables_count} 个数据表格）\n"
                    f"前序核心内容摘要：\n{text_sample}"
                )
                result["preview"] = result["summary"]
            except Exception as e:
                result["summary"] = f"Word 文档 ({format_file_size(size)})，直接解析提示: {str(e)[:100]}"
                result["preview"] = result["summary"]

        # 4. PDF 文档 (.pdf)
        elif ftype == "pdf":
            try:
                import pypdf
                reader = pypdf.PdfReader(file_path)
                page_count = len(reader.pages)
                first_page_text = ""
                if page_count > 0:
                    first_page_text = reader.pages[0].extract_text() or ""
                    if len(first_page_text) > 800:
                        first_page_text = first_page_text[:800] + "... [后续内容已截断]"
                result["meta"] = {
                    "page_count": page_count
                }
                result["summary"] = (
                    f"PDF 文档（共 {page_count} 页）\n"
                    f"首页内容摘要：\n{first_page_text.strip() or '（首页为扫描图像或无纯文本）'}"
                )
                result["preview"] = result["summary"]
            except Exception as e:
                result["summary"] = f"PDF 文档 ({format_file_size(size)})"
                result["preview"] = result["summary"]

        # 5. 图片文件 (png, jpg, jpeg, gif, webp, bmp)
        elif category == "image":
            try:
                from PIL import Image
                with Image.open(file_path) as img:
                    width, height = img.size
                    img_format = img.format or ftype.upper()
                    img_mode = img.mode

                result["meta"] = {
                    "width": width,
                    "height": height,
                    "format": img_format,
                    "mode": img_mode
                }

                # 极速轻量 OCR 文字抽取
                ocr_text = recognize_image_text(file_path)
                if ocr_text:
                    result["meta"]["ocr_text"] = ocr_text
                    result["summary"] = (
                        f"图片文件（分辨率: {width}x{height}，格式: {img_format}）\n"
                        f"【图片文字识别提取 (OCR 内容)】:\n{ocr_text}"
                    )
                else:
                    result["summary"] = f"图片文件（分辨率: {width}x{height}，格式: {img_format}，色彩空间: {img_mode}）"
                result["preview"] = result["summary"]
            except Exception as e:
                result["summary"] = f"图片文件 ({format_file_size(size)})"
                result["preview"] = result["summary"]

        # 6. 普通文本 / JSON / Markdown / 代码 / 日志
        else:
            text_content = read_text_with_encoding(file_path, max_chars=4000)
            lines = text_content.split("\n")
            line_count = len(lines)
            snippet = "\n".join(lines[:30])
            if len(snippet) > 1000:
                snippet = snippet[:1000] + "\n... [后续文本已截断]"

            result["meta"] = {
                "line_count": line_count,
                "preview_chars": len(snippet)
            }
            result["summary"] = f"文本/数据文件（共约 {line_count} 行，大小: {format_file_size(size)}）\n前序内容预览:\n```text\n{snippet}\n```"
            result["preview"] = result["summary"]

    except Exception as e:
        result["summary"] = f"文件 ({format_file_size(size)})，摘要提取异常: {str(e)[:100]}"
        result["preview"] = result["summary"]

    return result
