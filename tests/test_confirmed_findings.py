import base64
import io
import json
import os
import queue
import sys
import zipfile
import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app as app_module
from app import app
from core.agent.bot_manager import BotManager
from core.agent.tools import ReadFileTool
from core.live_util import generate_live_play_urls, generate_live_push_url
from core.registry import ApiRegistry


def test_read_file_tool_stays_inside_bot_uploads(tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "ok.txt").write_text("visible", encoding="utf-8")
    (uploads / "f_100_users.csv").write_text("user_id\n1\n", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("secret-value", encoding="utf-8")
    outside_link = uploads / "linked.txt"
    outside_link.symlink_to(secret)
    manager = SimpleNamespace(_uploads_dir=lambda _bot_id: str(uploads))
    tool = ReadFileTool("bot-1", cast(Any, manager))

    assert "visible" in tool.forward(str(uploads / "ok.txt"))
    assert "user_id" in tool.forward("users.csv")

    escaped = tool.forward(str(secret))
    traversed = tool.forward("../../secret.txt")
    linked = tool.forward("linked.txt")
    assert "secret-value" not in escaped
    assert "secret-value" not in traversed
    assert "secret-value" not in linked
    assert "不存在" in escaped


def test_reload_swaps_registry_without_clearing_live_map(monkeypatch):
    live = ApiRegistry()
    sentinel = cast(Any, object())
    live._apis = {"keep": sentinel}
    live._ordered_apis = [sentinel]
    live._discovered = True
    seen: dict[str, bool] = {}

    def fake_discover(self):
        seen["live_intact"] = live._apis.get("keep") is sentinel and bool(live._ordered_apis)
        self._apis = {"new": sentinel}
        self._ordered_apis = [sentinel]
        self._discovered = True

    monkeypatch.setattr(ApiRegistry, "_discover_unlocked", fake_discover)
    live.reload()

    assert seen["live_intact"] is True
    assert "new" in live._apis
    assert "keep" not in live._apis
    assert live._ordered_apis == [sentinel]


def test_xlsx_cells_keep_native_types_but_csv_stays_text():
    workbook = Workbook()
    sheet = workbook.active
    assert isinstance(sheet, Worksheet)
    sheet.append(["count", "enabled", "name"])
    sheet.append([123, True, "007"])
    buffer = io.BytesIO()
    workbook.save(buffer)

    rows, error = app_module._parse_batch_upload("batch.xlsx", buffer.getvalue())
    assert error is None
    assert rows[0]["count"] == 123
    assert rows[0]["enabled"] is True
    assert rows[0]["name"] == "007"

    csv_rows, csv_error = app_module._parse_batch_upload(
        "batch.csv",
        b"count,name\n123,007\n",
    )
    assert csv_error is None
    assert csv_rows[0]["count"] == "123"
    assert csv_rows[0]["name"] == "007"


def test_csv_formula_guard_strips_leading_whitespace_before_checking():
    assert app_module._safe_csv_cell("   =1+1") == "'   =1+1"
    assert app_module._safe_csv_cell("\t+SUM(A1:A2)") == "'\t+SUM(A1:A2)"
    assert app_module._safe_csv_cell("ordinary text") == "ordinary text"


def test_office_zip_validation_rejects_compression_bombs():
    """DOCX/XLSX ZIP containers are bounded before parser expansion."""
    from core.agent.file_parser import validate_zip_archive
    from core.agent.rag_manager import RagManager

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "A" * 1_000_000)
    bomb = payload.getvalue()

    with pytest.raises(ValueError, match="压缩比"):
        validate_zip_archive(bomb)
    with pytest.raises(ValueError, match="压缩比"):
        RagManager().extract_text_from_bytes("bomb.docx", bomb)


def test_office_zip_validation_allows_small_high_ratio_member():
    """Tiny repetitive XML is not a useful ZIP-bomb signal by itself."""
    from core.agent.file_parser import validate_zip_archive

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/styles.xml", " " * 10_000)

    # The member compresses far beyond 200:1 but remains below the absolute
    # ratio-check threshold, so legitimate Office documents are accepted.
    validate_zip_archive(payload.getvalue())


def test_batch_xlsx_validation_allows_small_high_ratio_member():
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/styles.xml", " " * 10_000)

    app_module._validate_xlsx_archive(payload.getvalue())


def test_office_zip_validation_rejects_member_count_limit():
    from core.agent.file_parser import validate_zip_archive

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("word/document.xml", "ok")
        archive.writestr("word/styles.xml", "ok")

    with pytest.raises(ValueError, match="成员数量"):
        validate_zip_archive(payload.getvalue(), max_members=1)


def test_batch_xlsx_path_applies_member_count_limit(monkeypatch):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("xl/workbook.xml", "ok")
        archive.writestr("xl/styles.xml", "ok")

    monkeypatch.setattr(app_module, "MAX_SPREADSHEET_ZIP_MEMBERS", 1)
    rows, error = app_module._parse_batch_upload("too-many.xlsx", payload.getvalue())
    assert rows == []
    assert error and "成员数量" in error


def test_read_file_docx_path_uses_office_zip_validation(tmp_path):
    """The attachment reader must validate DOCX before python-docx opens it."""
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "A" * 1_000_000)
    (uploads / "bomb.docx").write_bytes(payload.getvalue())
    manager = SimpleNamespace(_uploads_dir=lambda _bot_id: str(uploads))
    tool = ReadFileTool("bot-1", cast(Any, manager))

    result = tool.forward("bomb.docx")
    assert "读取 DOCX 失败" in result
    assert "压缩比" in result


def test_history_message_and_metadata_are_bounded(tmp_path):
    from core.agent.bot_manager import MAX_HISTORY_MESSAGE_BYTES

    manager = BotManager(data_dir=str(tmp_path / "data"))
    bot = manager.create_bot("bounded-history", "Bounded history")
    manager.append_history(
        bot.id,
        "user",
        "x" * (MAX_HISTORY_MESSAGE_BYTES + 1024),
        metadata={"payload": "y" * (MAX_HISTORY_MESSAGE_BYTES + 1024)},
    )

    message = manager.get_history(bot.id)[0]
    assert len(message["content"].encode("utf-8")) <= MAX_HISTORY_MESSAGE_BYTES
    assert message["metadata"]["_reason"] == "metadata_size_limit"


def test_file_export_truncates_at_aggregate_size_budget(tmp_path, monkeypatch):
    import apis.agent as agent_api

    manager = BotManager(data_dir=str(tmp_path / "data"))
    bot = manager.create_bot("bounded-export", "Bounded export")
    uploads = manager._uploads_dir(bot.id)
    os.makedirs(uploads, exist_ok=True)
    with open(os.path.join(uploads, "f_1_first.txt"), "wb") as handle:
        handle.write(b"1" * 700)
    with open(os.path.join(uploads, "f_2_second.txt"), "wb") as handle:
        handle.write(b"2" * 700)

    monkeypatch.setattr(agent_api, "MAX_AGENT_EXPORT_BYTES", 1024)
    monkeypatch.setattr(agent_api, "bot_manager", manager)
    response = agent_api.export_all_bot_files(bot.id)
    assert response.status_code == 200
    assert response.media_type == "application/zip"
    import zipfile
    async def collect_archive():
        return b"".join([chunk async for chunk in response.body_iterator])
    archive = asyncio.run(collect_archive())
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        assert zf.namelist() == ["first.txt"]


def test_chat_export_rejects_history_before_rendering(tmp_path, monkeypatch):
    import apis.agent as agent_api

    manager = BotManager(data_dir=str(tmp_path / "data"))
    bot = manager.create_bot("bounded-chat-export", "Bounded chat export")
    manager.append_history(bot.id, "user", "x" * 32)
    monkeypatch.setattr(agent_api, "MAX_AGENT_EXPORT_BYTES", 8)
    monkeypatch.setattr(agent_api, "bot_manager", manager)

    with pytest.raises(HTTPException) as exc_info:
        agent_api.export_bot_chat(bot.id, format="markdown")
    assert exc_info.value.status_code == 413


def test_html_chat_preview_sets_browser_defense_headers(tmp_path, monkeypatch):
    import apis.agent as agent_api

    manager = BotManager(data_dir=str(tmp_path / "data"))
    bot = manager.create_bot("header-export", "Header export")
    manager.append_history(bot.id, "user", "safe")
    monkeypatch.setattr(agent_api, "bot_manager", manager)

    response = agent_api.export_bot_chat(bot.id, format="html", preview=True)
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    import asyncio

    async def collect_body():
        return b"".join([chunk async for chunk in response.body_iterator]).decode("utf-8")

    body = asyncio.run(collect_body())
    assert 'http-equiv="Content-Security-Policy"' in body


@pytest.mark.parametrize("format_name", ["markdown", "html"])
def test_chat_export_streams_large_body_in_bounded_chunks(tmp_path, monkeypatch, format_name):
    """Large reports must not be returned as one full-size BytesIO chunk."""
    import asyncio
    import apis.agent as agent_api

    manager = BotManager(data_dir=str(tmp_path / "data"))
    bot = manager.create_bot("streaming-chat-export", "Streaming export")
    manager.append_history(bot.id, "user", "x" * (150 * 1024))
    monkeypatch.setattr(agent_api, "bot_manager", manager)

    response = agent_api.export_bot_chat(bot.id, format=format_name)

    async def collect_chunks():
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(collect_chunks())
    assert len(chunks) > 1
    assert max(map(len, chunks)) <= 64 * 1024
    assert sum(map(len, chunks)) > 150 * 1024


def test_batch_csv_export_streams_rows(monkeypatch):
    import asyncio
    import app as app_module

    rows = [{"id": index, "value": f"row-{index}"} for index in range(200)]
    monkeypatch.setattr(
        app_module.task_runner,
        "get_task",
        lambda _task_id: type("Task", (), {"results": rows})(),
    )
    response = app_module.export_csv("streaming-csv")

    async def collect_chunks():
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(collect_chunks())
    assert len(chunks) > 2  # BOM, header, and one or more data rows
    assert max(map(len, chunks)) < 64 * 1024
    body = b"".join(chunks)
    assert body.startswith(b"\xef\xbb\xbf")
    assert b"row-199" in body


def test_batch_request_bounds_direct_json_input(monkeypatch):
    from pydantic import ValidationError
    import app as app_module

    monkeypatch.setattr(app_module, "MAX_BATCH_INPUT_BYTES", 8)
    with pytest.raises(ValidationError):
        app_module.CreateBatchRequest(
            api_id="c2c_msg_withdraw",
            items=[{"payload": "123456789"}],
        )


def test_batch_history_detail_paginates_results(monkeypatch):
    import app as app_module

    rows = [{"id": index} for index in range(5)]
    monkeypatch.setattr(app_module, "load_history", lambda: [{"task_id": "paged", "results": rows}])

    response = app_module.get_history_detail("paged", offset=2, limit=2)
    data = response["data"]
    assert data["results"] == [{"id": 2}, {"id": 3}]
    assert data["result_total"] == 5
    assert data["result_has_more"] is True


def test_batch_results_jsonl_roundtrip(tmp_path, monkeypatch):
    import core.task_runner as task_runner_module

    monkeypatch.setattr(task_runner_module, "RESULTS_DIR", str(tmp_path / "batch_results"))
    task_runner_module.append_task_result("task-jsonl", {"id": 1, "_status": "OK"})
    task_runner_module.append_task_result("task-jsonl", {"id": 2, "_status": "FAIL"})

    assert task_runner_module.count_task_results("task-jsonl") == 2
    assert list(task_runner_module.iter_task_results("task-jsonl", offset=1, limit=1)) == [
        {"id": 2, "_status": "FAIL"}
    ]


def test_bot_history_uses_append_only_jsonl_with_legacy_json_migration(tmp_path):
    manager = BotManager(data_dir=str(tmp_path / "data"))
    bot = manager.create_bot("jsonl-history", "JSONL history")
    manager.append_history(bot.id, "user", "first")
    manager.append_history(bot.id, "assistant", "second")

    runtime_dir = manager._runtime_dir(bot.id)
    jsonl_path = os.path.join(runtime_dir, "history.jsonl")
    assert os.path.exists(jsonl_path)
    assert manager.get_history(bot.id, limit=2)[-1]["content"] == "second"

    legacy_bot = manager.create_bot("legacy-json-history", "Legacy history")
    legacy_dir = manager._runtime_dir(legacy_bot.id)
    os.makedirs(legacy_dir, exist_ok=True)
    with open(os.path.join(legacy_dir, "history.json"), "w", encoding="utf-8") as stream:
        json.dump([{"id": "legacy-1", "role": "user", "content": "legacy", "metadata": {}}], stream)
    assert manager.get_history(legacy_bot.id, limit=1)[0]["id"] == "legacy-1"
    assert os.path.exists(os.path.join(legacy_dir, "history.jsonl"))


def test_bot_history_offset_index_and_session_locks(tmp_path):
    manager = BotManager(data_dir=str(tmp_path / "data"))
    bot = manager.create_bot("indexed-history", "Indexed history", visibility="public")
    for index in range(8):
        manager.append_history(bot.id, "user", f"message-{index}")

    runtime_dir = manager._runtime_dir(bot.id)
    jsonl_path = os.path.join(runtime_dir, "history.jsonl")
    index_path = f"{jsonl_path}.idx"
    assert os.path.exists(index_path)
    with open(index_path, encoding="utf-8") as index_stream:
        entries = [json.loads(line) for line in index_stream]
    with open(jsonl_path, "rb") as jsonl_stream:
        raw_lines = jsonl_stream.read().splitlines(keepends=True)
    assert len(entries) == len(raw_lines) == 8
    for entry, raw in zip(entries, raw_lines):
        assert entry["end"] - entry["offset"] == len(raw)
        assert json.loads(raw.decode("utf-8"))["id"] == entry["id"]

    latest = manager.get_history_paged(bot.id, limit=3)
    assert [item["content"] for item in latest["history"]] == ["message-5", "message-6", "message-7"]
    previous = manager.get_history_paged(bot.id, limit=3, before_id=latest["history"][0]["id"])
    assert [item["content"] for item in previous["history"]] == ["message-2", "message-3", "message-4"]
    assert previous["has_more"] is True

    same_session_lock = manager._history_session_lock(bot.id)
    assert same_session_lock is manager._history_session_lock(bot.id)
    assert same_session_lock is not manager._history_session_lock(bot.id, user_id="another-user")

    # A same-sized malformed middle row must not make the offset map return
    # unrelated bytes; the read path rebuilds the index and skips that row.
    with open(jsonl_path, "rb") as jsonl_stream:
        rows = jsonl_stream.read().splitlines(keepends=True)
    rows[3] = b"!" * (len(rows[3]) - 1) + b"\n"
    with open(jsonl_path, "wb") as jsonl_stream:
        jsonl_stream.write(b"".join(rows))
    repaired = manager.get_history_paged(bot.id, limit=20)
    assert repaired["total"] == 7
    assert all(item["content"] != "message-3" for item in repaired["history"])

    # A crash after JSONL append but before newline/index fsync leaves a
    # partial tail.  Recovery must retain the valid prefix and discard only
    # that incomplete row.
    manager.append_history(bot.id, "user", "message-after-repair")
    with open(jsonl_path, "ab") as jsonl_stream:
        jsonl_stream.write(b'{"id":"crash-partial"')
    recovered = manager.get_history_paged(bot.id, limit=20)
    assert recovered["total"] == 8
    assert recovered["history"][-1]["content"] == "message-after-repair"


def test_chat_payload_rejects_unbounded_runtime_input():
    from pydantic import ValidationError
    from apis.agent import ChatPayload

    with pytest.raises(ValidationError):
        ChatPayload(message="x" * (1_048_576 + 1))
    with pytest.raises(ValidationError):
        ChatPayload(
            message="ok",
            attachments=[
                {
                    "id": str(index),
                    "name": "a.txt",
                    "path": "",
                }
                for index in range(21)
            ],
        )


def test_attachment_data_url_accepts_real_inline_image_payload_and_bounds_request_size():
    """The 3 MiB inline-image path expands to about 4 MiB in base64."""
    from pydantic import ValidationError
    from apis.agent import ChatPayload, MAX_ATTACHMENT_DATA_URL_LENGTH

    for raw_size in (1_500_000, 3 * 1024 * 1024):
        raw = b"\x89PNG\r\n\x1a\n" + b"x" * (raw_size - 8)
        data_url = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        payload = ChatPayload(
            message="preview",
            attachments=[
                {
                    "id": "inline-image",
                    "name": "preview.png",
                    "path": "/tmp/preview.png",
                    "data_url": data_url,
                }
            ],
        )
        assert payload.attachments[0].data_url == data_url
        assert len(data_url) < MAX_ATTACHMENT_DATA_URL_LENGTH

    with pytest.raises(ValidationError):
        ChatPayload(
            message="preview",
            attachments=[
                {
                    "id": "too-large",
                    "name": "preview.png",
                    "path": "/tmp/preview.png",
                    "data_url": "x" * (MAX_ATTACHMENT_DATA_URL_LENGTH + 1),
                }
            ],
        )


def test_file_export_limits_count_without_rejecting_the_bounded_prefix(tmp_path, monkeypatch):
    import apis.agent as agent_api

    manager = BotManager(data_dir=str(tmp_path / "data"))
    bot = manager.create_bot("bounded-count-export", "Bounded count export")
    uploads = manager._uploads_dir(bot.id)
    os.makedirs(uploads, exist_ok=True)
    for index in range(3):
        with open(os.path.join(uploads, f"f_{index}_report-{index}.txt"), "wb") as handle:
            handle.write(f"report-{index}".encode())

    monkeypatch.setattr(agent_api, "MAX_AGENT_FILES_PER_BOT", 2)
    monkeypatch.setattr(agent_api, "bot_manager", manager)
    response = agent_api.export_all_bot_files(bot.id)

    async def collect_body():
        return b"".join([chunk async for chunk in response.body_iterator])

    archive = zipfile.ZipFile(io.BytesIO(asyncio.run(collect_body())))
    assert archive.namelist() == ["report-0.txt", "report-1.txt"]


def test_generated_asset_quota_rejects_before_publish(tmp_path, monkeypatch):
    from core.agent import engine

    temp_path = tmp_path / "asset.tmp"
    destination = tmp_path / "asset.txt"
    temp_path.write_bytes(b"0123456789")
    monkeypatch.setattr(engine, "MAX_GENERATED_FILE_BYTES", 5)

    with pytest.raises(ValueError, match="100 MiB"):
        engine._commit_generated_asset(str(temp_path), str(destination), str(tmp_path))
    assert not destination.exists()
    assert not temp_path.exists()


def test_delete_bot_clears_sandboxes_for_every_user(tmp_path):
    from core.agent.engine import agent_engine

    manager = BotManager(data_dir=str(tmp_path / "data"))
    bot = manager.create_bot("sandbox_bot", "沙箱")
    agent_engine._remember_sandbox(f"{bot.id}:alice", {"marker": "alice"})
    agent_engine._remember_sandbox(f"{bot.id}:bob", {"marker": "bob"})
    agent_engine._remember_sandbox("other-bot:alice", {"marker": "keep"})
    try:
        assert manager.delete_bot(bot.id) is True
        assert agent_engine._get_sandbox(f"{bot.id}:alice") == {}
        assert agent_engine._get_sandbox(f"{bot.id}:bob") == {}
        assert agent_engine._get_sandbox("other-bot:alice")["marker"] == "keep"
    finally:
        agent_engine.clear_bot_sandboxes(bot.id)
        agent_engine.clear_bot_sandboxes("other-bot")


def test_sandbox_states_are_bounded_and_lru_ordered():
    from core.agent.engine import AgentEngine

    engine = AgentEngine()
    engine._sandbox_max_entries = 2
    engine._remember_sandbox("old", {"marker": "old"})
    engine._remember_sandbox("active", {"marker": "active"})
    assert engine._get_sandbox("old")["marker"] == "old"

    # Reading old refreshed it, so active is now the least recently used item.
    engine._remember_sandbox("new", {"marker": "new"})
    assert list(engine._bot_sandbox_states) == ["old", "new"]
    assert engine._get_sandbox("active") == {}


def test_live_url_helpers_reject_missing_domain():
    with pytest.raises(ValueError, match="domain is required"):
        generate_live_push_url(cast(Any, None), "stream")
    with pytest.raises(ValueError, match="domain is required"):
        generate_live_play_urls("  ", "stream")


def test_batch_sse_drains_queued_events_after_terminal_status(monkeypatch):
    events = queue.Queue()
    events.put({"type": "progress", "current": 1})
    events.put({"type": "complete", "success": 1})
    task = SimpleNamespace(
        status="COMPLETED",
        owner_id="local",
        register_subscriber=lambda: (events, []),
        unregister_subscriber=lambda _queue: None,
    )
    monkeypatch.setattr(app_module.task_runner, "get_task", lambda _task_id: task)

    with (
        TestClient(app) as client,
        client.stream("GET", "/api/batch/task-1/events") as response,
    ):
        assert response.status_code == 200
        body = "".join(response.iter_text())

    assert "progress" in body
    assert "complete" in body


def test_pytesseract_fallback_closes_image_file_handle(tmp_path, monkeypatch):
    """OCR fallback must close Pillow's descriptor-backed image before returning."""
    from PIL import Image

    from core.agent import file_parser

    image_path = tmp_path / "ocr.png"
    Image.new("RGB", (2, 2), "white").save(image_path)
    seen_images = []
    fake_pytesseract = SimpleNamespace(
        image_to_string=lambda image, **_: seen_images.append(image) or "recognized"
    )
    monkeypatch.setitem(sys.modules, "pytesseract", fake_pytesseract)
    monkeypatch.setattr(file_parser.shutil, "which", lambda _binary: None)

    assert file_parser.recognize_image_text(str(image_path)) == "recognized"
    assert seen_images and seen_images[0].fp is None
