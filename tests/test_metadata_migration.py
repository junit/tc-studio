from __future__ import annotations

import json
import os
from pathlib import Path


def test_json_metadata_migration_is_additive_and_skips_unattributed_partitions(tmp_path):
    from scripts.migrate_json_metadata_to_sqlite import migrate
    from core.persistence import SqliteMetadataMigrationStore

    data_root = tmp_path / "data"
    bot_dir = data_root / "bots" / "public-bot"
    (bot_dir / "skills").mkdir(parents=True)
    (bot_dir / "knowledge" / "files").mkdir(parents=True)
    (bot_dir / "users" / "unknown-hash").mkdir(parents=True)
    (bot_dir / "bot.json").write_text(
        json.dumps(
            {
                "id": "public-bot",
                "name": "Public",
                "owner_id": None,
                "visibility": "public",
                "created_at": "2026-11-06T00:00:00Z",
                "api_key": "must-not-enter-raw",
            }
        ),
        encoding="utf-8",
    )
    (bot_dir / "memory.json").write_text(
        json.dumps([{"key": "region", "content": "ap-guangzhou"}]),
        encoding="utf-8",
    )
    (bot_dir / "session_summary.json").write_text(
        json.dumps({"summary": "one summary"}),
        encoding="utf-8",
    )
    (bot_dir / "history.json").write_text(
        json.dumps([{"id": "m1", "role": "user", "content": "hello"}]),
        encoding="utf-8",
    )
    (bot_dir / "skills" / "inspect.json").write_text(
        json.dumps(
            {
                "name": "inspect",
                "description": "inspect",
                "input_schema": {},
                "created_at": "2026-11-06T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (bot_dir / "skills" / "inspect.py").write_text(
        "print('safe')\n",
        encoding="utf-8",
    )
    (bot_dir / "knowledge" / "docs.json").write_text(
        json.dumps(
            [
                {
                    "id": "doc-1",
                    "name": "guide.md",
                    "stored_name": "doc-1_guide.md",
                    "enabled": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    (bot_dir / "knowledge" / "chunks.json").write_text(
        json.dumps(
            [
                {
                    "doc_id": "doc-1",
                    "index": 0,
                    "content": "hello",
                    "tokens": ["hello"],
                }
            ]
        ),
        encoding="utf-8",
    )
    (bot_dir / "users" / "unknown-hash" / "memory.json").write_text(
        json.dumps([{"key": "private", "content": "unknown owner"}]),
        encoding="utf-8",
    )

    database = tmp_path / "tim.sqlite3"
    store = SqliteMetadataMigrationStore(str(database))
    report = migrate(
        data_root=data_root,
        store=store,
        apply=True,
        partition_map={},
    )

    assert report["bots_imported"] == 1
    assert report["memory_rows"] == 1
    assert report["summary_rows"] == 1
    assert report["skill_rows"] == 1
    assert report["conversation_messages"] == 1
    assert report["rag_documents"] == 1
    assert report["rag_chunks"] == 1
    assert report["skipped_unattributed_partitions"]
    counts = store.counts()
    assert counts["bots"] == 1
    assert counts["bot_memories"] == 1
    assert counts["bot_skills"] == 1
    assert counts["conversation_messages"] == 1
    assert counts["rag_documents"] == 1
    assert counts["rag_chunks"] == 1

    # Source files remain the rollback source.
    assert (bot_dir / "bot.json").exists()
    assert (bot_dir / "knowledge" / "docs.json").exists()
    connection = __import__("sqlite3").connect(database)
    try:
        definition = connection.execute(
            "SELECT definition_json FROM bots WHERE bot_id = 'public-bot'"
        ).fetchone()[0]
        assert "must-not-enter-raw" not in definition
        assert "[redacted]" in definition
    finally:
        connection.close()


def test_json_metadata_migration_disambiguates_legacy_rag_collisions(tmp_path):
    from scripts.migrate_json_metadata_to_sqlite import migrate
    from core.persistence import SqliteMetadataMigrationStore

    data_root = tmp_path / "data"
    bot_dir = data_root / "bots" / "legacy-bot"
    knowledge_dir = bot_dir / "knowledge"
    knowledge_dir.mkdir(parents=True)
    (bot_dir / "bot.json").write_text(
        json.dumps({"id": "legacy-bot", "name": "Legacy"}),
        encoding="utf-8",
    )
    (knowledge_dir / "docs.json").write_text(
        json.dumps(
            [
                {
                    "id": "legacy-doc",
                    "name": "a.txt",
                    "stored_name": "legacy-doc_a.txt",
                },
                {
                    "id": "legacy-doc",
                    "name": "b.txt",
                    "stored_name": "legacy-doc_b.txt",
                },
                {
                    "id": "single-doc",
                    "name": "c.txt",
                    "stored_name": "single-doc_c.txt",
                },
            ]
        ),
        encoding="utf-8",
    )
    (knowledge_dir / "chunks.json").write_text(
        json.dumps(
            [
                {
                    "doc_id": "legacy-doc",
                    "doc_name": "a.txt",
                    "index": 0,
                    "content": "a",
                },
                {
                    "doc_id": "legacy-doc",
                    "doc_name": "b.txt",
                    "index": 0,
                    "content": "b",
                },
                {
                    "doc_id": "single-doc",
                    "doc_name": "c.txt",
                    "index": 0,
                    "content": "c1",
                },
                {
                    "doc_id": "single-doc",
                    "doc_name": "c.txt",
                    "index": 0,
                    "content": "c2",
                },
            ]
        ),
        encoding="utf-8",
    )

    database = tmp_path / "tim.sqlite3"
    store = SqliteMetadataMigrationStore(str(database))
    report = migrate(
        data_root=data_root,
        store=store,
        apply=True,
        partition_map={},
    )

    assert report["rag_documents"] == 3
    assert report["source_rag_chunks"] == 4
    assert report["rag_chunks"] == 4
    assert report["rag_document_id_collision_groups"] == 1
    assert report["rag_documents_disambiguated"] == 1
    assert report["rag_chunk_index_collisions"] == 1
    assert report["ambiguous_rag_documents"] == []

    connection = __import__("sqlite3").connect(database)
    try:
        documents = connection.execute(
            """
            SELECT document_id, name
            FROM rag_documents
            WHERE bot_id = 'legacy-bot'
            ORDER BY name
            """
        ).fetchall()
        assert [row[1] for row in documents] == ["a.txt", "b.txt", "c.txt"]
        assert len({row[0] for row in documents}) == 3

        chunks = connection.execute(
            """
            SELECT d.name, c.chunk_index, c.content, c.metadata_json
            FROM rag_chunks c
            JOIN rag_documents d ON d.document_id = c.document_id
            ORDER BY d.name, c.chunk_index
            """
        ).fetchall()
        assert [(row[0], row[1], row[2]) for row in chunks] == [
            ("a.txt", 0, "a"),
            ("b.txt", 0, "b"),
            ("c.txt", 0, "c1"),
            ("c.txt", 1, "c2"),
        ]
        assert '"source_chunk_index":0' in chunks[-1][3]
    finally:
        connection.close()

    second_report = migrate(
        data_root=data_root,
        store=store,
        apply=True,
        partition_map={},
    )
    assert second_report["rag_documents"] == 3
    assert second_report["rag_chunks"] == 4
    assert store.counts()["rag_documents"] == 3
    assert store.counts()["rag_chunks"] == 4
