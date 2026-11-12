from __future__ import annotations

from types import SimpleNamespace
from io import BytesIO
from pathlib import Path
import socket

import pytest

from core.agent import engine as engine_module
from core.agent.bot_manager import BotManager


def test_binary_capability_payload_survives_spawn_rpc():
    received = []
    executor = engine_module.KillablePythonExecutor([], timeout_seconds=3)
    executor.additional_functions["save_file"] = lambda name, data: received.append((name, data))
    executor.send_tools({})
    try:
        executor("save_file('report.pdf', b'%PDF-1.4\\n' + b'\\x00\\xff' * 20)")
        assert received == [("report.pdf", b"%PDF-1.4\n" + b"\x00\xff" * 20)]
    finally:
        executor.close()


def test_binary_nsjail_transport_roundtrip_and_literal_envelope():
    from core.agent.nsjail_worker import _recv, _send

    value = {"kind": "response", "value": [b"\x00\xff", {"$tc_studio_wire": "bytes", "data": "literal"}]}
    left, right = socket.socketpair()
    try:
        engine_module._nsjail_send(left, value)
        assert _recv(right) == value
        _send(right, value)
        assert engine_module._nsjail_recv(left) == value
    finally:
        left.close()
        right.close()


def test_capability_transport_rejects_overflow_without_silently_truncating():
    from core.agent.executor_transport import decode_message, encode_message

    with pytest.raises(ValueError, match="上限"):
        encode_message({"args": [b"x" * 5000]}, 1000)
    with pytest.raises(ValueError, match="上限"):
        encode_message({"args": ["x" * 5000]}, 1000)
    values = {"args": list(range(1200))}
    assert decode_message(encode_message(values, 100_000), 100_000) == values


@pytest.fixture
def run_artifact_scenario(tmp_path, monkeypatch):
    """Exercise the real stream file helpers and persisted attachment metadata."""
    manager = BotManager(data_dir=str(tmp_path / "bots"))
    engine = engine_module.AgentEngine(manager)
    monkeypatch.setattr(engine, "create_model", lambda _cfg: object())
    monkeypatch.setattr(engine_module, "build_bot_tools", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(engine_module.time, "sleep", lambda _seconds: None)
    from core.agent.memory import memory_manager

    monkeypatch.setattr(memory_manager, "on_turn_completed", lambda **_kwargs: None)

    def run(scenario):
        class ReplayAgent:
            def __init__(self, **kwargs):
                self.python_executor = kwargs["executor"]
                self.state = {}
                self.memory = SimpleNamespace(steps=[])

            def run(self, _prompt, stream=True):
                for step in scenario(self.python_executor.additional_functions):
                    yield step if step is not None else engine_module.ActionOutput(None, False)
                yield engine_module.FinalAnswerStep("处理完成。")

        monkeypatch.setattr(engine_module, "CodeAgent", ReplayAgent)
        events = list(engine.run_agent_stream("default_assistant", "生成报告"))
        assert not [e for e in events if e["event"] == "error"]
        return next(e["data"] for e in events if e["event"] == "done")

    return run, manager, tmp_path


def test_stream_keeps_immutable_versions_and_drops_scratch_probes(run_artifact_scenario):
    run, manager, _ = run_artifact_scenario

    def scenario(scope):
        scope["save_file"]("report.txt", "old", publish=True)
        scope["save_file"]("_probe.txt", "probe")
        yield
        scope["save_file"]("report.txt", "updated content", publish=True)
        scope["save_file"]("_probe.txt", "")
        yield

    done = run(scenario)
    assert [(f["name"], f["size"]) for f in done["files"]] == [("report.txt", 3), ("report.txt", 15)]
    assert len({f["saved_name"] for f in done["files"]}) == 2
    assert [Path(f["path"]).read_text() for f in done["files"]] == ["old", "updated content"]
    metadata = manager.get_history("default_assistant")[-1]["metadata"]
    assert metadata["artifacts"] == done["files"]


def test_invalid_pdf_is_rejected_before_publication(run_artifact_scenario):
    run, manager, _ = run_artifact_scenario

    def scenario(scope):
        with pytest.raises(ValueError, match="PDF"):
            scope["save_file"]("broken.pdf", b"<bytes:6340>", publish=True)
        yield

    assert run(scenario)["files"] == []
    assert not (Path(manager._runtime_dir("default_assistant")) / "uploads/broken.pdf").exists()


def _unportable_cid_pdf(*, malformed: bool) -> bytes:
    """Same font graph as the last two failed reports, without real user data."""
    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject, TextStringObject,
    )

    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    descriptor = DictionaryObject({
        NameObject("/Type"): NameObject("/FontDescriptor"),
        NameObject("/FontName"): NameObject("/STSong-Light"),
        NameObject("/Flags"): NumberObject(6),
    })
    child = descriptor if malformed else DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/CIDFontType0"),
        NameObject("/BaseFont"): NameObject("/STSong-Light"),
        NameObject("/CIDSystemInfo"): DictionaryObject({
            NameObject("/Registry"): TextStringObject("Adobe"),
            NameObject("/Ordering"): TextStringObject("GB1"),
            NameObject("/Supplement"): NumberObject(2),
        }),
        NameObject("/FontDescriptor"): writer._add_object(descriptor),
    })
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type0"),
        NameObject("/BaseFont"): NameObject("/STSong-Light"),
        NameObject("/Encoding"): NameObject("/UniGB-UCS2-H"),
        NameObject("/DescendantFonts"): ArrayObject([writer._add_object(child)]),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
    })
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 42 750 Td <004100426d4b8bd5> Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize("malformed", [False, True], ids=["missing-embedded-cid-font", "descriptor-used-as-cid-font"])
def test_generated_pdf_rejects_unportable_or_malformed_font_before_publication(run_artifact_scenario, malformed):
    from pypdf import PdfReader

    data = _unportable_cid_pdf(malformed=malformed)
    # A page count and strict xref check alone incorrectly accepted both files.
    assert len(PdfReader(BytesIO(data), strict=True).pages) == 1
    run, manager, _ = run_artifact_scenario

    def scenario(scope):
        with pytest.raises(ValueError, match="字体"):
            scope["save_file"]("font-broken.pdf", data, publish=True)
        with scope["open"]("scratch.pdf", "wb") as stream:
            stream.write(data)
        with pytest.raises(ValueError, match="字体"):
            scope["publish_file"]("scratch.pdf")
        yield

    assert run(scenario)["files"] == []
    assert manager.metadata_store.list_files("default_assistant", **manager._metadata_scope("default_assistant")) == []


def test_application_startup_rejects_missing_pdf_runtime(monkeypatch):
    from fastapi.testclient import TestClient
    from app import app
    from core.agent import pdf_reports

    def missing_dependency(*_args, **_kwargs):
        raise ModuleNotFoundError("No module named 'reportlab'", name="reportlab")

    monkeypatch.setattr(pdf_reports, "render_pdf_report", missing_dependency)
    with pytest.raises(RuntimeError, match="uv run --locked"):
        with TestClient(app):
            pytest.fail("An incomplete runtime must not start serving requests")


def _assert_report_layout(reader):
    """Check actual serialized page coordinates, widths and vertical spacing."""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    for page in reader.pages:
        lines = []

        def check(text, cm, tm, font, size):
            if not text.strip():
                return
            x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
            y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
            width = stringWidth(text.rstrip("\n"), "TCStudioReportSansSC", size)
            assert 0 <= x and x + width <= float(page.mediabox.width) + 0.5
            assert 0 < y and y + size <= float(page.mediabox.height)
            if y > 30:  # footer is deliberately below the content frame
                lines.append((x, x + width, y, size))

        page.extract_text(visitor_text=check)
        for i, (left, right, y, size) in enumerate(lines):
            for other_left, other_right, other_y, other_size in lines[i + 1:]:
                if min(right, other_right) - max(left, other_left) > 1:
                    assert abs(y - other_y) >= max(size, other_size) * 0.9


def test_trusted_report_wraps_chinese_ascii_tables_and_embeds_font(tmp_path):
    from core.agent.pdf_reports import render_pdf_report, validate_pdf
    from pypdf import PdfReader

    content = "## 结论先行\n✅ 检查通过，评分：⭐⭐⭐\n" + ("中文诊断与长英文标识 " + "A" * 180 + "\n") * 30
    content += "\n| 检查项 | 结果 |\n|---|---|\n"
    content += "| 异常描述 | " + "长表格单元格需要自动分页。" * 80 + " |\n"
    content += "\n## 后续措施\n仅排版，不重新查询线上数据。"
    path = tmp_path / "report.pdf"
    path.write_bytes(render_pdf_report("📄 中文诊断报告", content))
    reader = PdfReader(path, strict=True)
    assert validate_pdf(path) == len(reader.pages) > 1
    text = "".join(page.extract_text() for page in reader.pages)
    assert "后续措施" in text and "中文诊断报告" in text
    assert "通过" in text and "★★★" in text
    for page in reader.pages:
        font = next(f.get_object() for f in page["/Resources"]["/Font"].values()
                    if "TCStudioReportSansSC" in f.get_object().get("/BaseFont", ""))
        assert font["/FontDescriptor"]["/FontFile2"].get_data()
    _assert_report_layout(reader)


def test_report_markup_is_literal_and_never_loads_remote_resources(monkeypatch):
    from core.agent.pdf_reports import render_pdf_report
    from pypdf import PdfReader
    import urllib.request

    def forbid_network(*_args, **_kwargs):
        pytest.fail("PDF rendering must not load remote resources")

    monkeypatch.setattr(urllib.request, "urlopen", forbid_network)
    content = '<img src="http://127.0.0.1/private">\n<font size="80">危险标签</font>'
    reader = PdfReader(BytesIO(render_pdf_report("安全排版", content)), strict=True)
    text = reader.pages[0].extract_text()
    assert "<img" in text and "<font" in text
    _assert_report_layout(reader)


def test_report_converts_simple_echarts_to_static_data_table():
    from core.agent.pdf_reports import render_pdf_report
    from pypdf import PdfReader

    content = '```echarts\n{"type":"line","title":"推流帧率","labels":["19:29:54","19:29:59"],"data":[13,34]}\n```'
    reader = PdfReader(BytesIO(render_pdf_report("诊断报告", content)))
    text = reader.pages[0].extract_text()
    assert "推流帧率" in text and "19:29:54" in text and "13" in text
    assert '"labels"' not in text and '"type"' not in text
    _assert_report_layout(reader)


@pytest.mark.parametrize("title,content", [("", "正文"), ("标题", ""), ("x" * 201, "正文"), ("标题", "x" * 200_001)])
def test_report_rejects_empty_or_oversized_input(title, content):
    from core.agent.pdf_reports import render_pdf_report

    with pytest.raises(ValueError):
        render_pdf_report(title, content)


def test_stream_pdf_helper_uses_server_layout_and_preserves_previous_valid_file(run_artifact_scenario):
    from pypdf import PdfReader

    run, _, _ = run_artifact_scenario
    saved = []

    def scenario(scope):
        saved.append(scope["create_pdf_report"]("report.pdf", "中文报告", "## 结论\n这是正式报告。"))
        with pytest.raises(ValueError, match="PDF"):
            scope["save_file"]("report.pdf", b"<bytes:6340>", publish=True)
        yield

    done = run(scenario)
    assert len(done["files"]) == 1
    artifact = done["files"][0]
    assert artifact["size"] == saved[0]["size"] == Path(artifact["path"]).stat().st_size > 1000
    assert "这是正式报告。" in PdfReader(artifact["path"]).pages[0].extract_text()
    assert not list(Path(artifact["path"]).parent.glob("*.tmp.pdf"))


def test_generated_pdf_download_matches_published_bytes(run_artifact_scenario, monkeypatch):
    from fastapi.testclient import TestClient
    from app import app
    import apis.agent as routes

    run, manager, _ = run_artifact_scenario

    def scenario(scope):
        scope["create_pdf_report"]("download.pdf", "中文报告", "正文内容与下载一致。")
        yield

    artifact = run(scenario)["files"][0]
    monkeypatch.setattr(routes, "bot_manager", manager)
    response = TestClient(app).get(artifact["download_url"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "attachment" in response.headers["content-disposition"]
    assert response.content == Path(artifact["path"]).read_bytes()


def test_stream_scan_excludes_invalid_pdf_and_atomic_temporary_files(run_artifact_scenario):
    run, _, _ = run_artifact_scenario

    def scenario(scope):
        for filename in ("incomplete.pdf", "report.123.456.tmp.txt"):
            with scope["open"](filename, "w") as stream:
                stream.write("not a complete report")
        yield

    assert run(scenario)["files"] == []


def test_pdf_report_capability_through_real_executor_with_polluted_globals(tmp_path):
    from core.agent.pdf_reports import render_pdf_report
    from pypdf import PdfReader

    path = tmp_path / "report.pdf"

    def create_pdf_report(filename, title, content):
        path.write_bytes(render_pdf_report(title, content))
        return {"filename": filename, "size": path.stat().st_size}

    executor = engine_module.KillablePythonExecutor([], timeout_seconds=3)
    executor.additional_functions["create_pdf_report"] = create_pdf_report
    executor.send_tools({})
    try:
        result = executor("size=22\nr=create_pdf_report('report.pdf', '中文报告', '## 结论\\n正常的 10.5pt 正文。')\nprint(r['size'])")
        assert int(result.logs.strip()) == path.stat().st_size
    finally:
        executor.close()
    reader = PdfReader(path)
    _assert_report_layout(reader)


def test_stream_keeps_real_print_observation_instead_of_no_output_notice(run_artifact_scenario):
    from smolagents.monitoring import Timing

    run, manager, _ = run_artifact_scenario

    def scenario(_scope):
        yield engine_module.ActionOutput(None, False)
        yield engine_module.ActionStep(
            step_number=1, timing=Timing(start_time=0, end_time=1),
            observations="Execution logs:\nPDF saved: 23456 bytes\nLast output from code snippet:\nNone",
        )

    run(scenario)
    logs = manager.get_history("default_assistant")[-1]["metadata"]["execution_logs"]
    assert any("PDF saved: 23456 bytes" in item["output"] for item in logs)
    assert not any("无标准输出" in item["output"] for item in logs)
