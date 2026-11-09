from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor

from core.agent.bot_manager import BotManager
from core.agent.memory import HierarchicalMemoryManager
from core.persistence import SqliteBotMetadataStore
from core.agent.rag_manager import RagManager
from core.runtime_context import runtime_user_id


def test_bot_runtime_metadata_is_sqlite_first(tmp_path):
    manager = BotManager(data_dir=str(tmp_path / "data"))
    bot = manager.create_bot("sqlite-first", "SQLite first")
    bot_dir = manager._bot_dir(bot.id)

    manager.save_memory(bot.id, "region", "ap-guangzhou")
    manager.append_history(bot.id, "user", "hello", metadata={"source": "test"})
    summary = HierarchicalMemoryManager().build_or_update_session_summary(
        bot.id,
        bot_dir,
        [{"id": "m-1", "role": "user", "content": "summary input"}],
        manager=manager,
    )
    skill = manager.save_skill(
        bot.id,
        "inspect",
        "inspect data",
        "def execute(**kwargs):\n    return 'ok'\n",
    )

    assert summary
    assert not os.path.exists(os.path.join(bot_dir, "bot.json"))
    assert not os.path.exists(os.path.join(bot_dir, "memory.json"))
    assert not os.path.exists(os.path.join(bot_dir, "history.json"))
    assert not os.path.exists(os.path.join(bot_dir, "history.jsonl"))
    assert not os.path.exists(os.path.join(bot_dir, "session_summary.json"))
    assert not os.path.exists(os.path.join(bot_dir, "skills", "inspect.json"))

    connection = manager.metadata_store._open()
    try:
        assert connection.execute(
            "SELECT 1 FROM bots WHERE bot_id = ?", (bot.id,)
        ).fetchone()
        assert connection.execute(
            "SELECT memory_key FROM bot_memories WHERE bot_id = ?",
            (bot.id,),
        ).fetchone()[0] == "region"
        assert connection.execute(
            "SELECT COUNT(*) FROM conversation_messages WHERE session_id = ?",
            (manager.metadata_store.session_id(bot.id),),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT 1 FROM bot_session_summaries WHERE bot_id = ?",
            (bot.id,),
        ).fetchone()
        stored_hash = connection.execute(
            "SELECT code_sha256 FROM bot_skills WHERE bot_id = ? AND name = ?",
            (bot.id, skill.name),
        ).fetchone()[0]
    finally:
        connection.close()
    assert stored_hash == hashlib.sha256(skill.code.encode("utf-8")).hexdigest()


def test_sqlite_rows_win_over_tampered_legacy_json(tmp_path):
    manager = BotManager(data_dir=str(tmp_path / "data"))
    bot = manager.create_bot("tamper-source", "Authoritative name")
    manager.save_memory(bot.id, "fact", "authoritative")
    manager.append_history(bot.id, "user", "authoritative history")
    manager.metadata_store.upsert_summary(
        bot_id=bot.id,
        summary={"summary": "authoritative summary"},
    )
    bot_dir = manager._bot_dir(bot.id)

    with open(os.path.join(bot_dir, "bot.json"), "w", encoding="utf-8") as stream:
        json.dump({"id": bot.id, "name": "forged name"}, stream)
    with open(os.path.join(bot_dir, "memory.json"), "w", encoding="utf-8") as stream:
        json.dump([{"key": "fact", "content": "forged memory"}], stream)
    with open(os.path.join(bot_dir, "history.json"), "w", encoding="utf-8") as stream:
        json.dump([{"id": "forged", "role": "user", "content": "forged history"}], stream)
    with open(os.path.join(bot_dir, "session_summary.json"), "w", encoding="utf-8") as stream:
        json.dump({"summary": "forged summary"}, stream)

    assert manager.get_bot(bot.id).name == "Authoritative name"
    assert manager.list_memories(bot.id)[0].content == "authoritative"
    assert manager.get_history(bot.id)[0]["content"] == "authoritative history"
    assert (
        HierarchicalMemoryManager()
        .get_session_summary(bot_dir, bot_id=bot.id, manager=manager)["summary"]
        == "authoritative summary"
    )


def test_legacy_json_is_imported_once_and_cannot_resurrect_deleted_state(tmp_path):
    manager = BotManager(data_dir=str(tmp_path / "data"))
    bot_dir = manager._bot_dir("legacy-runtime")
    os.makedirs(bot_dir, exist_ok=True)
    with open(os.path.join(bot_dir, "bot.json"), "w", encoding="utf-8") as stream:
        json.dump(
            {
                "id": "legacy-runtime",
                "name": "Legacy runtime",
                "visibility": "private",
            },
            stream,
        )
    with open(os.path.join(bot_dir, "memory.json"), "w", encoding="utf-8") as stream:
        json.dump([{"key": "legacy", "content": "legacy fact"}], stream)
    with open(os.path.join(bot_dir, "history.json"), "w", encoding="utf-8") as stream:
        json.dump(
            [{"id": "legacy-message", "role": "user", "content": "legacy history"}],
            stream,
        )

    assert manager.get_bot("legacy-runtime").name == "Legacy runtime"
    assert manager.list_memories("legacy-runtime")[0].content == "legacy fact"
    assert manager.get_history("legacy-runtime")[0]["id"] == "legacy-message"

    manager.delete_memory("legacy-runtime", "legacy")
    manager.clear_history("legacy-runtime")
    with open(os.path.join(bot_dir, "memory.json"), "w", encoding="utf-8") as stream:
        json.dump([{"key": "legacy", "content": "resurrected"}], stream)
    with open(os.path.join(bot_dir, "history.json"), "w", encoding="utf-8") as stream:
        json.dump(
            [{"id": "resurrected", "role": "user", "content": "resurrected"}],
            stream,
        )

    assert manager.list_memories("legacy-runtime") == []
    assert manager.get_history("legacy-runtime") == []


def test_public_bot_memory_is_scoped_by_runtime_subject(tmp_path):
    manager = BotManager(data_dir=str(tmp_path / "data"))
    bot = manager.create_bot("public-scope", "Public scope", visibility="public")

    alice = runtime_user_id.set("alice")
    try:
        manager.save_memory(bot.id, "private-fact", "alice-only")
        assert manager.list_memories(bot.id)[0].content == "alice-only"
    finally:
        runtime_user_id.reset(alice)

    bob = runtime_user_id.set("bob")
    try:
        assert manager.list_memories(bot.id) == []
    finally:
        runtime_user_id.reset(bob)


def test_rag_document_id_is_scoped_by_bot_and_owner(tmp_path):
    database = tmp_path / "tc_studio.sqlite3"
    store = SqliteBotMetadataStore(str(database))

    store.replace_rag_document(
        bot_id="bot-a",
        document={
            "id": "same-document-id",
            "name": "A document",
            "stored_name": "a.txt",
        },
        chunks=[{"index": 0, "content": "A secret"}],
        issuer="issuer-a",
        tenant_id="tenant-a",
        owner_subject="owner-a",
    )
    store.replace_rag_document(
        bot_id="bot-b",
        document={
            "id": "same-document-id",
            "name": "B document",
            "stored_name": "b.txt",
        },
        chunks=[{"index": 0, "content": "B secret"}],
        issuer="issuer-b",
        tenant_id="tenant-b",
        owner_subject="owner-b",
    )

    documents_a = store.list_rag_documents(
        "bot-a",
        issuer="issuer-a",
        tenant_id="tenant-a",
        owner_subject="owner-a",
    )
    documents_b = store.list_rag_documents(
        "bot-b",
        issuer="issuer-b",
        tenant_id="tenant-b",
        owner_subject="owner-b",
    )
    assert [item["name"] for item in documents_a] == ["A document"]
    assert [item["name"] for item in documents_b] == ["B document"]
    assert store.list_rag_chunks(
        "bot-a",
        issuer="issuer-a",
        tenant_id="tenant-a",
        owner_subject="owner-a",
    )[0]["content"] == "A secret"
    assert store.list_rag_chunks(
        "bot-b",
        issuer="issuer-b",
        tenant_id="tenant-b",
        owner_subject="owner-b",
    )[0]["content"] == "B secret"


def test_rag_stored_name_cannot_escape_bot_files_directory(tmp_path):
    data_root = tmp_path / "data" / "bots"
    rag = RagManager(data_root=str(data_root))
    outside = tmp_path / "outside.txt"
    outside.write_text("must survive", encoding="utf-8")

    rag.metadata_store.replace_rag_document(
        bot_id="path-bot",
        document={
            "id": "path-doc",
            "name": "path.txt",
            "stored_name": "../outside.txt",
        },
        chunks=[],
    )

    assert rag.get_document_file_path("path-bot", "path-doc") is None
    assert rag.delete_document("path-bot", "path-doc") is True
    assert outside.read_text(encoding="utf-8") == "must survive"


def test_rag_legacy_metadata_is_imported_before_new_document_write(tmp_path):
    data_root = tmp_path / "data" / "bots"
    rag = RagManager(data_root=str(data_root))
    bot_dir = data_root / "legacy-race" / "knowledge"
    bot_dir.mkdir(parents=True)
    (bot_dir / "docs.json").write_text(
        json.dumps(
            [
                {
                    "id": "legacy-doc",
                    "name": "legacy.txt",
                    "stored_name": "legacy-doc_legacy.txt",
                    "enabled": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    (bot_dir / "chunks.json").write_text(
        json.dumps(
            [
                {
                    "doc_id": "legacy-doc",
                    "index": 0,
                    "content": "legacy content",
                    "tokens": ["legacy"],
                }
            ]
        ),
        encoding="utf-8",
    )

    rag.add_document("legacy-race", "new.txt", b"new content")
    names = {item["name"] for item in rag.list_documents("legacy-race")}
    assert names == {"legacy.txt", "new.txt"}
    assert not (bot_dir / "docs.json").exists()
    assert not (bot_dir / "chunks.json").exists()


def test_guideline_appends_from_independent_managers_are_not_lost(tmp_path):
    data_dir = tmp_path / "data"
    creator = BotManager(data_dir=str(data_dir))
    bot = creator.create_bot("guideline-race", "Guideline race")
    managers = [BotManager(data_dir=str(data_dir)) for _ in range(2)]

    def append(index: int):
        return managers[index].add_learned_guideline(
            bot.id,
            f"rule-{index}",
            f"reason-{index}",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        created = list(executor.map(append, (0, 1)))

    assert all(item is not None for item in created)
    loaded = BotManager(data_dir=str(data_dir)).get_bot(bot.id)
    assert loaded is not None
    assert {item.instruction for item in loaded.learned_guidelines} == {
        "rule-0",
        "rule-1",
    }


def test_skill_source_hash_mismatch_is_diagnostic_and_blocks_execution(tmp_path):
    from core.agent.tools import RunSkillTool
    from core.tim_client import TimClient

    manager = BotManager(data_dir=str(tmp_path / "data"))
    bot = manager.create_bot("skill-integrity", "Skill integrity")
    skill = manager.save_skill(
        bot.id,
        "safe",
        "safe skill",
        "def execute(**kwargs):\n    return 'safe'\n",
    )
    skill_path = os.path.join(manager._skills_dir(bot.id), "safe.py")
    with open(skill_path, "w", encoding="utf-8") as stream:
        stream.write("def execute(**kwargs):\n    return 'tampered'\n")

    loaded = manager.get_skill(bot.id, "safe")
    assert loaded is not None
    assert loaded.integrity_status == "mismatch"
    assert loaded.integrity_error == "技能源码哈希不匹配"

    result = RunSkillTool(bot.id, manager, TimClient()).forward("safe")
    assert result["success"] is False
    assert "完整性校验失败" in result["error"]
