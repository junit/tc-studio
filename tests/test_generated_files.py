from __future__ import annotations

from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
import csv
import hashlib
import json
import multiprocessing
import sqlite3
import zipfile

import pytest
from fastapi.testclient import TestClient

from core.agent.bot_manager import BotManager
from core.agent.engine import _confined_open, _save_generated_file
from core.agent.generated_files import GeneratedFileSession
from core.auth import CurrentUser
from core.runtime_context import AuthScope, runtime_auth_scope, runtime_user_id


@contextmanager
def identity(subject, issuer="https://issuer-a.example", tenant="tenant-a"):
    token = runtime_user_id.set(subject)
    scope_token = runtime_auth_scope.set(AuthScope(issuer, tenant, subject))
    try:
        yield
    finally:
        runtime_auth_scope.reset(scope_token)
        runtime_user_id.reset(token)


@pytest.fixture
def file_session(tmp_path):
    manager = BotManager(data_dir=str(tmp_path / "data"))
    session = GeneratedFileSession(manager, "default_assistant", _save_generated_file, _confined_open)
    try:
        yield session, manager
    finally:
        session.close()


@pytest.mark.parametrize("name,data", [
    ("说明.txt", "中文文本"), ("说明.md", "# 报告\n正文"),
    ("数据.json", {"用户": [1, 2]}),
    ("数据.csv", [{"用户": "张三", "次数": 2}]),
    ("数据.xlsx", [{"用户": "张三", "次数": 2}]),
    ("报告.docx", "# 中文标题\n中文正文"),
])
def test_common_formats_are_real_readable_and_indexed(file_session, name, data):
    session, manager = file_session
    result = session.save_file(name, data, publish=True)
    path = Path(result["path"])
    payload = path.read_bytes()
    ext = path.suffix
    if ext in {".txt", ".md"}:
        assert payload.decode() == data
    elif ext == ".json":
        assert json.loads(payload) == data
    elif ext == ".csv":
        assert list(csv.DictReader(payload.decode("utf-8-sig").splitlines())) == [{"用户": "张三", "次数": "2"}]
    elif ext == ".xlsx":
        from openpyxl import load_workbook

        book = load_workbook(BytesIO(payload))
        assert list(book.active.values) == [("用户", "次数"), ("张三", 2)]
        book.close()
    elif ext == ".docx":
        from docx import Document

        assert [p.text for p in Document(BytesIO(payload)).paragraphs] == ["中文标题", "中文正文"]
    rows = manager.metadata_store.list_files("default_assistant")
    assert len(rows) == 1
    assert rows[0]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert rows[0]["size_bytes"] == len(payload) == result["size"]
    assert len(manager.list_bot_files("default_assistant")) == 1
    assert path.stat().st_mode & 0o777 == 0o600


def test_scratch_probes_are_not_published_downloads_and_are_removed(file_session, monkeypatch):
    from app import app
    from apis import agent as routes

    session, manager = file_session
    scratch = session.save_file("probe.bin", b"\x00\xff")
    session.save_file("probe.txt", "hello")
    with session.open("raw.txt", "w") as stream:
        stream.write("debug")
    assert "url" not in scratch
    assert session.files == []
    assert manager.list_bot_files("default_assistant") == []
    monkeypatch.setattr(routes, "bot_manager", manager)
    client = TestClient(app)
    assert client.get("/api/agent/bots/default_assistant/files/probe.txt").status_code == 404
    with pytest.raises(ValueError, match="BIN"):
        session.publish_file("probe.bin")
    session.close()
    assert not Path(scratch["path"]).exists()


def test_explicit_publication_uses_stable_links_and_cannot_be_overwritten(file_session, monkeypatch):
    from app import app
    from apis import agent as routes

    session, manager = file_session
    session.save_file("report #1?.txt", "first")
    first = session.publish_file("report #1?.txt")
    second = session.save_file("report #1?.txt", "second", publish=True)
    assert first["saved_name"] != second["saved_name"]
    with pytest.raises(PermissionError):
        session.open(first["path"], "w")
    monkeypatch.setattr(routes, "bot_manager", manager)
    client = TestClient(app)
    assert client.get(first["download_url"]).content == b"first"
    assert client.get(second["download_url"]).content == b"second"
    assert client.get(first["download_url"]).headers["cache-control"] == "no-store"
    listed = client.get("/api/agent/bots/default_assistant/files").json()["files"]
    assert {f["name"] for f in listed} == {first["saved_name"], second["saved_name"]}


def test_exact_storage_name_wins_over_newer_original_name_alias(file_session, monkeypatch):
    from app import app
    from apis import agent as routes

    session, manager = file_session
    first = session.save_file("report.txt", "old immutable content", publish=True)
    second = session.save_file(first["saved_name"], "different content", publish=True)
    monkeypatch.setattr(routes, "bot_manager", manager)
    client = TestClient(app)
    assert client.get(first["download_url"]).content == b"old immutable content"
    assert client.delete(first["url"]).status_code == 200
    assert client.get(second["download_url"]).content == b"different content"
    assert len(manager.metadata_store.list_files("default_assistant")) == 1


def test_common_file_helpers_roundtrip_through_real_executor_rpc(file_session):
    from core.agent.engine import KillablePythonExecutor
    from openpyxl import load_workbook
    from docx import Document

    session, _ = file_session
    executor = KillablePythonExecutor([], timeout_seconds=5)
    executor.additional_functions.update({"save_file": session.save_file, "publish_file": session.publish_file})
    executor.send_tools({})
    try:
        executor("save_file('report.xlsx', [{'用户': '张三', '值': 8}], publish=True)\nsave_file('report.docx', '# 中文标题\\n正文', publish=True)")
    finally:
        executor.close()
    workbook = load_workbook(session.files[0]["path"])
    assert list(workbook.active.values) == [("用户", "值"), ("张三", 8)]
    workbook.close()
    assert Document(session.files[1]["path"]).paragraphs[0].text == "中文标题"


def test_scratch_open_roundtrip_through_real_executor_rpc(file_session):
    from core.agent.engine import KillablePythonExecutor

    session, _ = file_session
    executor = KillablePythonExecutor([], timeout_seconds=5)
    executor.additional_functions.update({"open": session.open, "publish_file": session.publish_file})
    executor.send_tools({})
    try:
        output = executor("with open('working.txt', 'w', encoding='utf-8') as f:\n    f.write('中文工作文件')\nwith open('working.txt', 'r', encoding='utf-8') as f:\n    print(f.read())\npublish_file('working.txt')")
        assert "中文工作文件" in output.logs
    finally:
        executor.close()
    assert Path(session.files[0]["path"]).read_text() == "中文工作文件"


def test_failed_sql_registration_rolls_back_bytes_and_reservation(file_session, monkeypatch):
    session, manager = file_session

    def fail(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(manager, "register_bot_file", fail)
    with pytest.raises(RuntimeError, match="database unavailable"):
        session.save_file("report.txt", "content", publish=True)
    assert session.files == []
    assert not list(Path(session.uploads_dir).glob("g_*"))
    with sqlite3.connect(manager.database_path) as connection:
        assert connection.execute("select count(*) from bot_file_reservations").fetchone()[0] == 0


@pytest.mark.parametrize("name,data", [
    ("report.xlsx", {"not": "a table"}), ("report.docx", {"not": "a document"}),
    ("report.docx", b"not a DOCX"), ("report.xlsx", b"not an XLSX"),
    ("report.pptx", [1, 2]), ("report.pptx", b"not a PPTX"),
    ("report.zip", b"not a ZIP"), ("report.pdf", b"<bytes:6340>"),
    ("report.png", "not an image"), ("report.json", "not json"),
    ("report.xls", [[1]]), ("report.bin", b"probe"),
])
def test_mislabeled_or_invalid_formats_fail_before_publication(file_session, name, data):
    session, manager = file_session
    with pytest.raises((ValueError, json.JSONDecodeError)):
        session.save_file(name, data, publish=True)
    assert session.files == []
    assert manager.metadata_store.list_files("default_assistant") == []


def test_csv_and_excel_untrusted_cells_do_not_become_formulas(file_session):
    from openpyxl import load_workbook

    session, _ = file_session
    dangerous = ["=HYPERLINK(\"https://example.invalid\")", "+cmd", "-cmd", "@SUM(1)", "  =SUM(1)", "\t=1"]
    csv_result = session.save_file("data.csv", [[value] for value in dangerous] + [[-3]], publish=True)
    rows = list(csv.reader(Path(csv_result["path"]).read_text(encoding="utf-8-sig").splitlines()))
    assert [row[0] for row in rows] == ["'" + value for value in dangerous] + ["-3"]
    excel = session.save_file("data.xlsx", [[value] for value in dangerous] + [[-3]], publish=True)
    workbook = load_workbook(excel["path"])
    assert [row[0].data_type for row in workbook.active] == ["s"] * len(dangerous) + ["n"]
    workbook.close()


@pytest.mark.parametrize("data", [[["x" * 32768]], [[float("nan")]], [[float("inf")]], [[1] * 201]])
def test_table_limits_fail_without_silent_truncation(file_session, data):
    session, _ = file_session
    with pytest.raises(ValueError):
        session.save_file("data.xlsx", data, publish=True)


def test_legacy_outputs_reconcile_with_existing_uploaded_rows(file_session):
    session, manager = file_session
    uploaded = session.save_file("uploaded.txt", "input", publish=True)
    legacy = Path(session.uploads_dir) / "old_report.txt"
    legacy.write_text("legacy generated output")
    (legacy.parent / "_probe.txt").write_text("probe")
    (legacy.parent / "orphan.bin").write_bytes(b"probe")
    (legacy.parent / "g_uncommitted_orphan.txt").write_text("not committed to SQL")
    listed = manager.list_bot_files("default_assistant")
    assert {item["name"] for item in listed} == {uploaded["saved_name"], legacy.name}
    assert len(manager.list_bot_files("default_assistant")) == 2


def test_authenticated_subject_local_does_not_resolve_development_root(tmp_path):
    manager = BotManager(data_dir=str(tmp_path / "data"))
    root = manager._runtime_dir("default_assistant")
    with identity("local"):
        assert manager._runtime_dir("default_assistant") != root
        assert manager._metadata_scope("default_assistant")["owner_subject"] == "local"
        assert manager.get_history("default_assistant") == []
    assert manager._runtime_dir("default_assistant") == root


def test_claiming_local_private_bot_retains_existing_sqlite_resources(tmp_path):
    manager = BotManager(data_dir=str(tmp_path / "data"))
    bot = manager.create_bot("legacy", "Legacy", visibility="private")
    manager.append_history(bot.id, "user", "previous conversation")
    manager.save_memory(bot.id, "test", "previous memory")
    guideline = manager.add_learned_guideline(bot.id, "previous guideline")
    bot.owner_id = "local"
    manager.save_bot(bot)
    assert manager.get_history(bot.id)[0]["content"] == "previous conversation"
    assert manager.list_memories(bot.id)[0].content == "previous memory"
    assert manager.get_bot(bot.id).learned_guidelines[0].id == guideline.id
    assert next(item for item in manager.list_bots() if item["id"] == bot.id)["guidelines_count"] == 1
    assert manager.remove_learned_guideline(bot.id, guideline.id)


def _publish_same_name(data_dir, queue):
    manager = BotManager(data_dir=data_dir)
    session = GeneratedFileSession(manager, "default_assistant", _save_generated_file, _confined_open)
    try:
        result = session.save_file("report.txt", "complete version", publish=True)
        queue.put(result["saved_name"])
    finally:
        session.close()


def test_two_processes_publish_same_name_without_overwriting(tmp_path):
    manager = BotManager(data_dir=str(tmp_path / "data"))
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    workers = [context.Process(target=_publish_same_name, args=(manager.data_dir, queue)) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
        if worker.is_alive():
            worker.kill()
            worker.join(timeout=3)
        assert worker.exitcode == 0
    names = {queue.get(timeout=2) for _ in workers}
    assert len(names) == 2
    assert {row["stored_name"] for row in manager.metadata_store.list_files("default_assistant")} == names
    assert all((Path(manager._uploads_dir("default_assistant")) / name).read_text() == "complete version" for name in names)
    queue.close()
