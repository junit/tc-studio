"""Migrate structured BOT/RAG metadata from JSON files into the unified SQLite DB.

The command is intentionally additive:

* without ``--apply`` it only scans and reports what would be imported;
* with ``--apply`` it upserts metadata and keeps the original files untouched;
* public BOT ``users/<hash>`` partitions are skipped unless an explicit
  ``--partition-map`` assigns the hash to a verified subject.

Large source files and executable skill code stay in the local Blob/file layer.
SQLite receives ownership, status, summaries, indexes and integrity metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.persistence import (  # noqa: E402
    SqliteAuditStore,
    SqliteMetadataMigrationStore,
)


SENSITIVE_KEY_PARTS = (
    "secret",
    "token",
    "password",
    "authorization",
    "api_key",
    "private_key",
    "usersig",
)


def _safe_copy(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return "[truncated]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:200]:
            key = str(raw_key)[:128]
            lowered = key.lower().replace("-", "_")
            if any(part in lowered for part in SENSITIVE_KEY_PARTS):
                result[key] = "[redacted]"
            else:
                result[key] = _safe_copy(raw_value, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_copy(item, depth=depth + 1) for item in value[:200]]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)[:512]


def _read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, TypeError, ValueError):
        return default


def _read_history(runtime_dir: Path) -> list[dict[str, Any]]:
    jsonl = runtime_dir / "history.jsonl"
    if jsonl.is_file():
        rows: list[dict[str, Any]] = []
        try:
            with jsonl.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(item, dict):
                        rows.append(item)
        except OSError:
            return []
        return rows
    legacy = _read_json(runtime_dir / "history.json", [])
    return [item for item in legacy if isinstance(item, dict)] if isinstance(legacy, list) else []


def _scope_for_partition(
    bot: Mapping[str, Any],
    *,
    bot_id: str,
    runtime_dir: Path,
    data_root: Path,
    partition_map: Mapping[str, Any],
) -> tuple[str, str, str] | None:
    relative = runtime_dir.relative_to(data_root / "bots" / bot_id)
    if not relative.parts:
        return (
            str(bot.get("issuer") or ""),
            str(bot.get("tenant_id") or ""),
            str(bot.get("owner_id") or ""),
        )
    if relative.parts[0] != "users" or len(relative.parts) != 2:
        return None
    lookup_key = f"{bot_id}/users/{relative.parts[1]}"
    mapped = partition_map.get(lookup_key)
    if not isinstance(mapped, Mapping) or not str(mapped.get("subject") or "").strip():
        return None
    return (
        str(mapped.get("issuer") or bot.get("issuer") or ""),
        str(mapped.get("tenant_id") or bot.get("tenant_id") or ""),
        str(mapped["subject"]).strip(),
    )


def _sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _original_upload_name(stored_name: str) -> str:
    return re.sub(r"^f_\d+_", "", str(stored_name))


def _document_name_candidates(document: Mapping[str, Any]) -> set[str]:
    candidates: set[str] = set()
    for key in ("name", "stored_name"):
        value = str(document.get(key) or "").strip()
        if value:
            candidates.add(value)
            candidates.add(Path(value).name)
    return candidates


def _chunk_name_candidates(chunk: Mapping[str, Any]) -> set[str]:
    candidates: set[str] = set()
    for key in ("doc_name", "title"):
        value = str(chunk.get(key) or "").strip()
        if value:
            candidates.add(value)
            candidates.add(Path(value).name)
    return candidates


def _assign_duplicate_document_chunks(
    documents: list[Mapping[str, Any]],
    chunks: list[Mapping[str, Any]],
) -> tuple[list[list[Mapping[str, Any]]], bool]:
    """Assign legacy chunks to duplicate document rows without guessing.

    Older JSON snapshots can contain several documents with the same ID.  A
    chunk's ``doc_name`` or ``title`` is the only reliable discriminator in
    that format.  Chunks that match zero or multiple documents are left out of
    the import and reported for manual review instead of being silently
    attached to the wrong document.
    """
    assignments: list[list[Mapping[str, Any]]] = [
        [] for _ in documents
    ]
    ambiguous = False
    for chunk in chunks:
        names = _chunk_name_candidates(chunk)
        matches = [
            index
            for index, document in enumerate(documents)
            if names & _document_name_candidates(document)
        ]
        if len(matches) != 1:
            ambiguous = True
            continue
        assignments[matches[0]].append(chunk)
    return assignments, ambiguous


def _count_chunk_index_collisions(chunks: list[Mapping[str, Any]]) -> int:
    used: set[int] = set()
    next_index = 0
    collisions = 0
    for position, chunk in enumerate(chunks):
        raw_index = chunk.get("index", position)
        try:
            requested_index = int(raw_index)
        except (TypeError, ValueError):
            requested_index = position
        chunk_index = requested_index
        if chunk_index < 0 or chunk_index in used:
            collisions += 1
            chunk_index = max(next_index, 0)
            while chunk_index in used:
                chunk_index += 1
        used.add(chunk_index)
        next_index = max(next_index, chunk_index + 1)
    return collisions


def _disambiguated_document_id(
    *,
    original_id: str,
    bot_id: str,
    occurrence: int,
    document: Mapping[str, Any],
) -> str:
    identity = json.dumps(
        {
            "bot_id": bot_id,
            "document_id": original_id,
            "occurrence": occurrence,
            "name": document.get("name"),
            "stored_name": document.get("stored_name"),
            "created_at": document.get("created_at"),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{original_id}~legacy-{digest}"


def migrate(
    *,
    data_root: Path,
    store: SqliteMetadataMigrationStore,
    apply: bool,
    partition_map: Mapping[str, Any],
) -> dict[str, Any]:
    bots_root = data_root / "bots"
    report: dict[str, Any] = {
        "data_root": str(data_root),
        "apply": apply,
        "bots_seen": 0,
        "bots_imported": 0,
        "memory_rows": 0,
        "summary_rows": 0,
        "skill_rows": 0,
        "file_rows": 0,
        "conversation_messages": 0,
        "rag_documents": 0,
        "rag_chunks": 0,
        "source_rag_chunks": 0,
        "rag_chunk_index_collisions": 0,
        "rag_document_id_collision_groups": 0,
        "rag_documents_disambiguated": 0,
        "ambiguous_rag_documents": [],
        "skipped_unattributed_partitions": [],
        "invalid_files": [],
    }
    if not bots_root.is_dir():
        return report

    used_document_ids: set[str] = set()
    for bot_json in sorted(bots_root.glob("*/bot.json")):
        bot = _read_json(bot_json, None)
        if not isinstance(bot, dict) or not bot.get("id"):
            report["invalid_files"].append(str(bot_json))
            continue
        bot_id = str(bot["id"])
        report["bots_seen"] += 1
        issuer = str(bot.get("issuer") or "")
        tenant_id = str(bot.get("tenant_id") or "")
        owner_subject = str(bot.get("owner_id") or "")
        definition = _safe_copy(bot)
        if apply:
            store.upsert_bot(
                {
                    "bot_id": bot_id,
                    "issuer": issuer,
                    "tenant_id": tenant_id,
                    "owner_subject": owner_subject,
                    "visibility": bot.get("visibility") or "private",
                    "definition": definition,
                    "created_at": bot.get("created_at"),
                    "updated_at": bot.get("updated_at"),
                }
            )
        report["bots_imported"] += 1

        bot_dir = bot_json.parent
        runtime_dirs = [bot_dir]
        users_dir = bot_dir / "users"
        if users_dir.is_dir():
            for partition_dir in sorted(path for path in users_dir.iterdir() if path.is_dir()):
                runtime_dirs.append(partition_dir)

        for runtime_dir in runtime_dirs:
            scope = _scope_for_partition(
                bot,
                bot_id=bot_id,
                runtime_dir=runtime_dir,
                data_root=data_root,
                partition_map=partition_map,
            )
            if scope is None:
                if runtime_dir != bot_dir:
                    report["skipped_unattributed_partitions"].append(str(runtime_dir))
                continue
            part_issuer, part_tenant, part_owner = scope

            memories = _read_json(runtime_dir / "memory.json", [])
            if isinstance(memories, list):
                for memory in memories:
                    if not isinstance(memory, dict):
                        continue
                    report["memory_rows"] += 1
                    if apply:
                        store.upsert_memory(
                            bot_id=bot_id,
                            memory=memory,
                            issuer=part_issuer,
                            tenant_id=part_tenant,
                            owner_subject=part_owner,
                        )

            summary = _read_json(runtime_dir / "session_summary.json", None)
            if isinstance(summary, dict):
                report["summary_rows"] += 1
                if apply:
                    store.upsert_summary(
                        bot_id=bot_id,
                        summary=summary,
                        issuer=part_issuer,
                        tenant_id=part_tenant,
                        owner_subject=part_owner,
                    )

            messages = _read_history(runtime_dir)
            if messages:
                report["conversation_messages"] += len(messages)
                if apply:
                    session_key = (
                        f"{bot_id}|{part_issuer}|{part_tenant}|{part_owner}|"
                        f"{runtime_dir.relative_to(bot_dir)}"
                    )
                    session_id = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
                    store.replace_conversation(
                        session_id=session_id,
                        bot_id=bot_id,
                        messages=messages,
                        issuer=part_issuer,
                        tenant_id=part_tenant,
                        owner_subject=part_owner,
                    )

            uploads_dir = runtime_dir / "uploads"
            if uploads_dir.is_dir():
                for blob_path in sorted(uploads_dir.iterdir()):
                    if (
                        not blob_path.is_file()
                        or blob_path.is_symlink()
                    ):
                        continue
                    report["file_rows"] += 1
                    if apply:
                        digest = _sha256(blob_path)
                        stat = blob_path.stat()
                        relative_blob = blob_path.relative_to(data_root)
                        file_id = hashlib.sha256(
                            (
                                f"{bot_id}|{part_issuer}|{part_tenant}|"
                                f"{part_owner}|{blob_path.name}|{digest or ''}"
                            ).encode("utf-8")
                        ).hexdigest()[:32]
                        store.upsert_file(
                            {
                                "file_id": file_id,
                                "bot_id": bot_id,
                                "issuer": part_issuer,
                                "tenant_id": part_tenant,
                                "owner_subject": part_owner,
                                "blob_uri": f"local://data/{relative_blob.as_posix()}",
                                "original_name": _original_upload_name(blob_path.name),
                                "stored_name": blob_path.name,
                                "content_type": None,
                                "size_bytes": stat.st_size,
                                "sha256": digest,
                                "created_at": time.strftime(
                                    "%Y-%m-%dT%H:%M:%S%z",
                                    time.localtime(stat.st_mtime),
                                ),
                            }
                        )

        skills_dir = bot_dir / "skills"
        if skills_dir.is_dir():
            for meta_path in sorted(skills_dir.glob("*.json")):
                skill = _read_json(meta_path, None)
                if not isinstance(skill, dict) or not skill.get("name"):
                    report["invalid_files"].append(str(meta_path))
                    continue
                code_path = skills_dir / f"{skill['name']}.py"
                report["skill_rows"] += 1
                if apply:
                    store.upsert_skill(
                        bot_id=bot_id,
                        skill=skill,
                        code_uri=f"local://data/bots/{bot_id}/skills/{code_path.name}",
                        code_sha256=_sha256(code_path),
                        issuer=issuer,
                        tenant_id=tenant_id,
                        owner_subject=owner_subject,
                    )

        docs_path = bot_dir / "knowledge" / "docs.json"
        chunks_path = bot_dir / "knowledge" / "chunks.json"
        docs = _read_json(docs_path, [])
        chunks = _read_json(chunks_path, [])
        chunks_by_doc: dict[str, list[Mapping[str, Any]]] = {}
        if isinstance(chunks, list):
            for chunk in chunks:
                if isinstance(chunk, dict) and chunk.get("doc_id"):
                    chunks_by_doc.setdefault(str(chunk["doc_id"]), []).append(chunk)
        if isinstance(docs, list):
            documents_by_id: dict[str, list[Mapping[str, Any]]] = {}
            for document in docs:
                if not isinstance(document, dict) or not document.get("id"):
                    continue
                document_id = str(document["id"])
                documents_by_id.setdefault(document_id, []).append(document)

            for original_document_id, document_group in documents_by_id.items():
                source_chunks = chunks_by_doc.get(original_document_id, [])
                report["source_rag_chunks"] += len(source_chunks)
                if len(document_group) > 1:
                    report["rag_document_id_collision_groups"] += 1
                if len(document_group) == 1:
                    assigned_chunks = [source_chunks]
                    ambiguous = False
                else:
                    assigned_chunks, ambiguous = _assign_duplicate_document_chunks(
                        document_group,
                        source_chunks,
                    )

                for occurrence, document in enumerate(document_group):
                    report["rag_documents"] += 1
                    doc_chunks = assigned_chunks[occurrence]
                    report["rag_chunks"] += len(doc_chunks)
                    report["rag_chunk_index_collisions"] += _count_chunk_index_collisions(
                        doc_chunks
                    )
                    if ambiguous:
                        report["ambiguous_rag_documents"].append(
                            f"{bot_id}:{original_document_id}:{document.get('name') or occurrence}"
                        )

                    document_id = original_document_id
                    if document_id in used_document_ids:
                        document_id = _disambiguated_document_id(
                            original_id=original_document_id,
                            bot_id=bot_id,
                            occurrence=occurrence,
                            document=document,
                        )
                        suffix = 2
                        base_document_id = document_id
                        while document_id in used_document_ids:
                            document_id = f"{base_document_id}-{suffix}"
                            suffix += 1
                        report["rag_documents_disambiguated"] += 1
                    used_document_ids.add(document_id)

                    document_for_store = dict(document)
                    document_for_store["id"] = document_id
                    if document_id != original_document_id:
                        document_for_store["legacy_document_id"] = original_document_id
                    prepared_chunks = []
                    for chunk in doc_chunks:
                        prepared_chunk = dict(chunk)
                        prepared_chunk["source_document_id"] = original_document_id
                        prepared_chunk["doc_id"] = document_id
                        prepared_chunks.append(prepared_chunk)

                    if apply:
                        stored_name = document.get("stored_name")
                        blob_uri = (
                            f"local://data/bots/{bot_id}/knowledge/files/{stored_name}"
                            if stored_name
                            else None
                        )
                        store.replace_rag_document(
                            bot_id=bot_id,
                            document=document_for_store,
                            chunks=prepared_chunks,
                            issuer=issuer,
                            tenant_id=tenant_id,
                            owner_subject=owner_subject,
                            blob_uri=blob_uri,
                        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=PROJECT_ROOT / "data",
        help="JSON data root; defaults to ./data",
    )
    parser.add_argument("--database", type=str, default=None)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write additive SQLite rows; source files are never deleted",
    )
    parser.add_argument(
        "--partition-map",
        type=Path,
        default=None,
        help="JSON mapping such as {'bot/users/hash': {'subject': 'user-1', ...}}",
    )
    args = parser.parse_args()
    partition_map = _read_json(args.partition_map, {}) if args.partition_map else {}
    if not isinstance(partition_map, dict):
        raise SystemExit("--partition-map must contain a JSON object")
    store = SqliteMetadataMigrationStore(args.database)
    report = migrate(
        data_root=args.data_root.resolve(),
        store=store,
        apply=args.apply,
        partition_map=partition_map,
    )
    report["sqlite_counts"] = store.counts() if args.apply else None
    if args.apply:
        SqliteAuditStore(args.database).append(
            action="metadata.migration",
            resource_type="migration",
            outcome="SUCCESS",
            metadata={
                "bots_imported": report["bots_imported"],
                "memory_rows": report["memory_rows"],
                "skill_rows": report["skill_rows"],
                "conversation_messages": report["conversation_messages"],
                "rag_documents": report["rag_documents"],
                "skipped_unattributed_partitions": len(
                    report["skipped_unattributed_partitions"]
                ),
            },
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
