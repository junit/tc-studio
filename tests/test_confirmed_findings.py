import io
import os
import queue
import sys
from types import SimpleNamespace
from typing import Any, cast

import pytest
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
