"""Server-owned PDF authoring. Model code supplies text, never fonts or HTML."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import json
import re
import sys
import threading
import unicodedata
from xml.sax.saxutils import escape

from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, StreamObject

MAX_REPORT_CHARS = 200_000
MAX_REPORT_PAGES = 500
_FONT_NAME = "TCStudioReportSansSC"
_FONT_PATH = Path(__file__).with_name("assets") / "fonts/TCStudioReportSansSC-Regular.ttf"
_FONT_LOCK = threading.Lock()
_STANDARD_PDF_FONTS = frozenset({
    "/Courier", "/Courier-Bold", "/Courier-Oblique", "/Courier-BoldOblique",
    "/Helvetica", "/Helvetica-Bold", "/Helvetica-Oblique", "/Helvetica-BoldOblique",
    "/Times-Roman", "/Times-Bold", "/Times-Italic", "/Times-BoldItalic", "/Symbol", "/ZapfDingbats",
})


def _validate_pdf_fonts(reader: PdfReader) -> None:
    """A readable xref/page tree does not validate the font resource graph.

    Generated PDFs must embed nonstandard fonts so viewer-specific fallback
    cannot change their glyphs and spacing. Standard PDF fonts stay supported.
    Forms have their own resources, which need the same checks as pages.
    """
    visited = set()

    def dictionary(value):
        value = value.get_object() if value is not None else None
        return value if isinstance(value, DictionaryObject) else DictionaryObject()

    def inspect(resources):
        resources = dictionary(resources)
        if id(resources) in visited:
            return
        visited.add(id(resources))
        if len(visited) > 2048:
            raise ValueError("PDF 字体资源层级过多")
        for reference in dictionary(resources.get("/Font")).values():
            font = reference.get_object()
            if not isinstance(font, DictionaryObject) or font.get("/Type") != "/Font":
                raise ValueError("PDF 字体对象类型错误")
            subtype = font.get("/Subtype")
            if subtype == "/Type3":
                # Type3 glyphs are embedded drawing programs, not system fonts.
                if not dictionary(font.get("/CharProcs")):
                    raise ValueError("PDF Type3 字体缺少字形")
                inspect(font.get("/Resources", DictionaryObject()))
                continue
            if subtype == "/Type0":
                descendants = font.get("/DescendantFonts")
                descendants = descendants.get_object() if descendants is not None else None
                if not isinstance(descendants, ArrayObject) or len(descendants) != 1:
                    raise ValueError("PDF CID 字体缺少唯一的子字体")
                font = descendants[0].get_object()
                if (not isinstance(font, DictionaryObject) or font.get("/Type") != "/Font"
                        or font.get("/Subtype") not in {"/CIDFontType0", "/CIDFontType2"}):
                    raise ValueError("PDF CID 字体引用错误，不能引用 FontDescriptor 作为子字体")
                if not dictionary(font.get("/CIDSystemInfo")):
                    raise ValueError("PDF CID 字体缺少字符集信息")
            elif subtype == "/Type1" and font.get("/BaseFont") in _STANDARD_PDF_FONTS:
                continue
            elif subtype not in {"/Type1", "/TrueType", "/MMType1"}:
                raise ValueError("PDF 字体类型不受支持")
            descriptor = font.get("/FontDescriptor")
            descriptor = descriptor.get_object() if descriptor is not None else None
            if not isinstance(descriptor, DictionaryObject) or descriptor.get("/Type") != "/FontDescriptor":
                raise ValueError("PDF 字体缺少有效描述")
            if not any(isinstance(descriptor.get(key).get_object(), StreamObject)
                       for key in ("/FontFile", "/FontFile2", "/FontFile3") if descriptor.get(key) is not None):
                raise ValueError("PDF 非标准字体未嵌入，请使用 create_pdf_report 生成可移植报告")
        for reference in dictionary(resources.get("/XObject")).values():
            form = reference.get_object()
            if isinstance(form, DictionaryObject) and form.get("/Subtype") == "/Form":
                inspect(form.get("/Resources", DictionaryObject()))

    for page in reader.pages:
        inspect(page.get("/Resources", DictionaryObject()))


def validate_pdf(path: str | Path) -> int:
    """Reject incomplete/placeholder PDFs before their atomic publication."""
    try:
        with open(path, "rb") as stream:
            if not stream.read(8).startswith(b"%PDF-"):
                raise ValueError("缺少 PDF 文件头")
            stream.seek(0, 2)
            size = stream.tell()
            if size > 64 * 1024 * 1024:
                raise ValueError("PDF 文件超过 64 MiB 限制")
            stream.seek(max(0, size - 1024))
            if b"%%EOF" not in stream.read():
                raise ValueError("PDF 尚未写入完成")
            stream.seek(0)
            reader = PdfReader(stream, strict=True)
            if reader.is_encrypted:
                raise ValueError("生成的 PDF 不应加密")
            count = len(reader.pages)
            if not 0 < count <= MAX_REPORT_PAGES:
                raise ValueError("PDF 页数必须在 1 到 500 之间")
            _validate_pdf_fonts(reader)
            return count
    except Exception as exc:
        raise ValueError(f"PDF 校验失败：{exc}") from exc


def _report_font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    with _FONT_LOCK:
        if _FONT_NAME not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(_FONT_NAME, str(_FONT_PATH)))
        return pdfmetrics.getFont(_FONT_NAME)


def check_pdf_runtime() -> None:
    """Exercise the actual authoring dependency and bundled font before serving."""
    try:
        if not _FONT_PATH.is_file():
            raise FileNotFoundError("缺少随应用分发的 PDF 中文字体")
        data = render_pdf_report("PDF 就绪检查", "中文与 English 123 字体检查。")
        reader = PdfReader(BytesIO(data), strict=True)
        _validate_pdf_fonts(reader)
        if len(reader.pages) != 1 or "English 123" not in reader.pages[0].extract_text():
            raise ValueError("PDF 自检未生成可读正文")
    except Exception as exc:
        raise RuntimeError(
            f"PDF 运行环境自检失败（解释器 {sys.executable}）：{exc}。"
            "请在项目目录执行 uv sync --locked，并使用 uv run --locked python app.py 启动；"
            "pip 部署需用同一个解释器安装 requirements.lock。"
        ) from exc


def _normalize_text(text: str, supported: dict) -> str:
    replacements = {"⚠": "注意", "✅": "✓", "❌": "×", "⭐": "★", "\u00a0": " "}
    result = []
    for char in text:
        if char in replacements:
            result.append(replacements[char])
        elif char in "\n\t" or ord(char) in supported:
            result.append(char)
        elif unicodedata.category(char) in {"Cf", "Mn"} or 0x1F000 <= ord(char) <= 0x1FAFF:
            # Decorative emoji are absent from the bundled text font. Omit
            # them instead of silently drawing a missing-glyph square.
            continue
        elif unicodedata.category(char).startswith("C"):
            result.append(" ")
        else:
            raise ValueError(f"报告字体不支持字符 U+{ord(char):04X}；请改用文字描述")
    return "".join(result)


def _static_chart_tables(content: str) -> str:
    """Preserve simple ECharts values as tables rather than printing JSON code."""
    def replace(match):
        try:
            chart = json.loads(match.group(1))
        except ValueError:
            return match.group(0)
        if not isinstance(chart, dict):
            return match.group(0)
        labels, values = chart.get("labels"), chart.get("data")
        if not isinstance(labels, list) or not isinstance(values, list):
            return match.group(0)
        if not labels or len(labels) != len(values) or len(labels) > 1000:
            return match.group(0)
        if any(not isinstance(value, (str, int, float)) for value in labels + values):
            return match.group(0)

        def cell(value):
            return str(value).replace("|", "／").replace("\n", " ").replace("\r", " ")

        title = cell(chart.get("title") or "图表数据")
        rows = [f"### {title}", "| 项目 | 数值 |", "|---|---|"]
        rows.extend(f"| {cell(label)} | {cell(value)} |" for label, value in zip(labels, values))
        return "\n".join(rows)

    return re.sub(r"```echarts\s*\n(.*?)\n```", replace, content, flags=re.DOTALL)


def render_pdf_report(title: str, content: str) -> bytes:
    """Render a bounded Markdown subset with embedded Chinese font and wrapping.

    Raw HTML, images, remote resources, executable CSS and custom font paths
    are never evaluated. Headings, paragraphs, lists and pipe tables suffice
    for diagnostic reports and keep the capability small.
    """
    if not isinstance(title, str) or not isinstance(content, str):
        raise ValueError("PDF 标题与正文必须是字符串")
    if not title.strip() or not content.strip():
        raise ValueError("PDF 标题与正文不能为空")
    if len(title) > 200 or len(content) > MAX_REPORT_CHARS:
        raise ValueError("PDF 标题最多 200 字符，正文最多 200000 字符")

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

    font = _report_font()
    title = _normalize_text(title, font.face.charToGlyph)
    content = _normalize_text(_static_chart_tables(content.replace("\r\n", "\n").replace("\r", "\n")), font.face.charToGlyph)
    if not title.strip() or not content.strip():
        raise ValueError("PDF 需要包含可显示的标题与正文")
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=42, rightMargin=42,
        topMargin=42, bottomMargin=44, title=title, author="TC Studio",
    )
    base = ParagraphStyle(
        "ReportBody", fontName=_FONT_NAME, fontSize=10.5, leading=17,
        spaceAfter=7, alignment=TA_LEFT, wordWrap="CJK", splitLongWords=True,
        textColor=colors.HexColor("#263348"),
    )
    styles = {
        0: base,
        1: ParagraphStyle("ReportTitle", parent=base, fontSize=19, leading=28, spaceAfter=16),
        2: ParagraphStyle("ReportHeading", parent=base, fontSize=13, leading=21,
                          spaceBefore=12, spaceAfter=7, keepWithNext=True,
                          textColor=colors.HexColor("#174b78")),
        3: ParagraphStyle("ReportCell", parent=base, fontSize=9, leading=14, spaceAfter=0),
    }

    def paragraph(text: str, kind: int = 0):
        # Escape first: even link-looking HTML and ReportLab tags remain text.
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
        text = escape(text)
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"`([^`]+)`", r"\1", text)
        return Paragraph(text or " ", styles[kind])

    story = [paragraph(title, 1)]
    lines = content.splitlines()
    index = 0
    in_code = False
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if line.startswith("```"):
            in_code = not in_code
            continue
        if not line:
            continue
        if not in_code and re.fullmatch(r"[-*_]{3,}", line):
            story.append(Spacer(1, 5))
            continue
        if not in_code and line.startswith("|") and line.endswith("|"):
            rows = []
            while True:
                cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line[1:-1])]
                if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                    rows.append(cells)
                if index >= len(lines) or not (lines[index].strip().startswith("|") and lines[index].strip().endswith("|")):
                    break
                line = lines[index].strip()
                index += 1
            if rows:
                columns = max(map(len, rows))
                if columns > 12 or len(rows) > 1000:
                    raise ValueError("PDF 表格最多 12 列、1000 行")
                data = [[paragraph(cell, 3) for cell in row + [""] * (columns - len(row))] for row in rows]
                table = LongTable(
                    data, colWidths=[doc.width / columns] * columns,
                    repeatRows=1, splitByRow=1, splitInRow=1, hAlign="LEFT",
                )
                table.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf1f7")),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d5dee8")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]))
                story.extend([table, Spacer(1, 9)])
            continue
        heading = re.match(r"^#{1,6}\s+(.+)$", line) if not in_code else None
        if heading:
            story.append(paragraph(heading.group(1), 2))
        else:
            if not in_code:
                line = re.sub(r"^>\s?", "", line)
            line = re.sub(r"^[-*+]\s+", "· ", line) if not in_code else line
            story.append(paragraph(line))

    def footer(canvas, _doc):
        if canvas.getPageNumber() > MAX_REPORT_PAGES:
            raise ValueError("PDF 报告超过 500 页限制")
        canvas.saveState()
        canvas.setFont(_FONT_NAME, 8)
        canvas.setFillColor(colors.HexColor("#61728a"))
        canvas.drawString(42, 23, "TC Studio")
        canvas.drawRightString(A4[0] - 42, 23, f"第 {canvas.getPageNumber()} 页")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
