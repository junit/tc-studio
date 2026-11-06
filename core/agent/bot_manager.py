# ruff: noqa: BLE001, S110, S112, UP006, UP015, UP017, UP035, UP045

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
import weakref
from collections import deque
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

from core.runtime_context import runtime_user_id

from .constants import HIGH_RISK_API_KEYWORDS
from .file_parser import format_file_size, get_file_category_and_type
from .models import (
    BotConfig,
    BotStats,
    LearnedGuideline,
    LLMConfig,
    MemoryItem,
    SkillItem,
    now_iso,
)

MAX_HISTORY_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_HISTORY_METADATA_BYTES = 2 * 1024 * 1024
MAX_HISTORY_TOTAL_BYTES = 32 * 1024 * 1024
MAX_RUNTIME_FILES = 1000
MAX_RUNTIME_TOTAL_FILE_BYTES = 512 * 1024 * 1024


def _truncate_history_text(value: Any, limit: int = MAX_HISTORY_MESSAGE_BYTES) -> str:
    text = str(value or "")
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    suffix = "\n[内容超过单条历史消息限制，已截断]"
    return encoded[: max(0, limit - len(suffix.encode("utf-8")))].decode("utf-8", "ignore") + suffix


def _bound_history_metadata(metadata: dict | None) -> dict[str, Any]:
    candidate = metadata if isinstance(metadata, dict) else {}
    try:
        encoded = json.dumps(candidate, ensure_ascii=False, default=str).encode("utf-8")
    except (TypeError, ValueError):
        return {"_truncated": True, "_reason": "metadata_not_serializable"}
    if len(encoded) <= MAX_HISTORY_METADATA_BYTES:
        return candidate
    return {
        "_truncated": True,
        "_reason": "metadata_size_limit",
        "_original_size_bytes": len(encoded),
    }


def _estimate_history_entry_bytes(message: dict[str, Any]) -> int:
    """Estimate persisted size without materializing the complete history."""
    content_size = len(str(message.get("content") or "").encode("utf-8"))
    try:
        metadata_size = len(
            json.dumps(
                message.get("metadata") or {},
                ensure_ascii=False,
                default=str,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except (TypeError, ValueError):
        metadata_size = MAX_HISTORY_METADATA_BYTES
    return content_size + metadata_size + 256


def _trim_history_to_budget(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total = sum(_estimate_history_entry_bytes(item) for item in history)
    while len(history) > 1 and total > MAX_HISTORY_TOTAL_BYTES:
        total -= _estimate_history_entry_bytes(history.pop(0))
    return history


def _atomic_dump_json(filepath: str, data: Any, indent: int = 2, default: Any | None = None) -> None:
    """原子化安全写入 JSON：先写入临时文件，确保序列化完全成功后再原子替换目标文件。
    彻底避免因异常、不可序列化对象或意外断电导致历史会话文件被清空截断为 0 字节。
    """
    parent_dir = os.path.dirname(filepath)
    os.makedirs(parent_dir, mode=0o700, exist_ok=True)  # pi-lens-ignore: unchecked-throwing-call-python -- persistence boundary
    with suppress(OSError):
        os.chmod(parent_dir, 0o700)
    tmp_path = f"{filepath}.{os.getpid()}.{threading.get_ident()}.{int(time.time() * 1000)}.tmp"
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent, default=default)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, filepath)
    except Exception:
        with suppress(OSError):
            os.remove(tmp_path)
        raise


class BotManager:
    def __init__(self, data_dir: str | None = None):
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        self.data_dir = data_dir or os.path.join(base_dir, "data")
        self.bots_dir = os.path.join(self.data_dir, "bots")
        self.llm_config_path = os.path.join(self.data_dir, "llm_config.json")
        self._lock = threading.RLock()
        # Locks are per runtime path; weak values avoid retaining one lock for
        # every short-lived authenticated session forever.
        self._history_locks: weakref.WeakValueDictionary[str, threading.RLock] = weakref.WeakValueDictionary()
        os.makedirs(self.bots_dir, exist_ok=True)  # pi-lens-ignore: unchecked-throwing-call-python -- persistence boundary
        self._ensure_default_bot()

    # -------------------------------------------------------------
    # LLM 全局模型配置
    # -------------------------------------------------------------
    def get_llm_config(self) -> LLMConfig:
        if os.path.exists(self.llm_config_path):
            try:
                with open(self.llm_config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return LLMConfig(**data)
            except Exception:
                pass
        return LLMConfig()

    def save_llm_config(self, config: LLMConfig):
        with self._lock:
            _atomic_dump_json(self.llm_config_path, config.model_dump())

    def get_masked_llm_config(self) -> dict[str, Any]:
        cfg = self.get_llm_config().model_dump()
        key = cfg.get("api_key", "")
        if key:
            if len(key) > 8:
                cfg["api_key_masked"] = key[:3] + "..." + key[-4:]
            else:
                cfg["api_key_masked"] = "***"
        else:
            cfg["api_key_masked"] = ""
        # The raw credential must never cross the API boundary.
        cfg["api_key"] = ""
        cfg["has_api_key"] = bool(key)
        return cfg

    # -------------------------------------------------------------
    # 安全基线与高危操作审计规则
    # -------------------------------------------------------------
    def get_high_risk_keywords(self) -> list[str]:
        """获取当前配置的高危接口关键词列表"""
        sec_path = os.path.join(self.data_dir, "security_config.json")
        if os.path.exists(sec_path):
            try:
                with open(sec_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    kws = data.get("high_risk_keywords")
                    if isinstance(kws, list) and kws:
                        return [str(k).strip().lower() for k in kws if str(k).strip()]
            except Exception:
                pass
        return list(HIGH_RISK_API_KEYWORDS)

    def set_high_risk_keywords(self, keywords: list[str]) -> list[str]:
        """保存自定义高危接口关键词列表"""
        with self._lock:
            sec_path = os.path.join(self.data_dir, "security_config.json")
            clean_kws = list(dict.fromkeys([str(k).strip().lower() for k in keywords if str(k).strip()]))
            _atomic_dump_json(sec_path, {"high_risk_keywords": clean_kws})
            return clean_kws

    # -------------------------------------------------------------
    # BOT 实体管理
    # -------------------------------------------------------------
    def _bot_dir(self, bot_id: str) -> str:
        # 净化合法路径字符
        clean_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", bot_id)
        return os.path.join(self.bots_dir, clean_id)

    def _runtime_dir(self, bot_id: str, user_id: str | None = None) -> str:
        """Return per-user mutable state directory for public Bots."""
        bot_dir = self._bot_dir(bot_id)
        bot = self.get_bot(bot_id)
        effective_user_id = user_id or runtime_user_id.get()
        if bot and bot.visibility == "public" and effective_user_id != "local":
            partition = hashlib.sha256(effective_user_id.encode("utf-8")).hexdigest()[:24]
            return os.path.join(bot_dir, "users", partition)
        return bot_dir

    def _history_path(self, bot_id: str, user_id: str | None = None) -> str:
        return os.path.join(self._runtime_dir(bot_id, user_id), "history.json")

    def _history_jsonl_path(self, bot_id: str, user_id: str | None = None) -> str:
        """Append-only session history path used by new writes."""
        return os.path.join(self._runtime_dir(bot_id, user_id), "history.jsonl")

    def _history_index_path(self, bot_id: str, user_id: str | None = None) -> str:
        return f"{self._history_jsonl_path(bot_id, user_id)}.idx"

    def _history_session_lock(self, bot_id: str, user_id: str | None = None) -> threading.RLock:
        """Return a stable lock for one bot/user history stream."""
        key = os.path.abspath(self._runtime_dir(bot_id, user_id))
        with self._lock:
            lock = self._history_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._history_locks[key] = lock
            return lock

    def _rebuild_history_index_unlocked(self, jsonl_path: str, index_path: str | None = None) -> None:
        index_path = index_path or f"{jsonl_path}.idx"
        tmp_path = f"{index_path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(jsonl_path, "rb") as source, open(tmp_path, "w", encoding="utf-8") as index:
                while True:
                    offset = source.tell()
                    raw = source.readline()
                    if not raw:
                        break
                    try:
                        item = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, TypeError, ValueError):
                        continue
                    if not isinstance(item, dict):
                        continue
                    index.write(json.dumps({"id": item.get("id", ""), "offset": offset, "end": source.tell()}, ensure_ascii=False))
                    index.write("\n")
                index.flush()
                os.fsync(index.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, index_path)
        except Exception:
            with suppress(OSError):
                os.remove(tmp_path)
            raise

    def _load_history_index_unlocked(
        self, jsonl_path: str, index_path: str | None = None, *, rebuild_on_error: bool = True
    ) -> list[dict[str, Any]]:
        index_path = index_path or f"{jsonl_path}.idx"
        try:
            file_size = os.path.getsize(jsonl_path)
            entries: list[dict[str, Any]] = []
            with open(index_path, "r", encoding="utf-8") as index:
                for line in index:
                    item = json.loads(line)
                    if isinstance(item, dict) and isinstance(item.get("offset"), int) and isinstance(item.get("end"), int):
                        entries.append(item)
            previous_end = 0
            for item in entries:
                offset = item["offset"]
                end = item["end"]
                if offset < previous_end or end <= offset or end > file_size:
                    raise ValueError("history index is invalid")
                previous_end = end
            last_end = entries[-1].get("end", 0) if entries else 0
            incomplete_tail = False
            if last_end != file_size:
                # A process can die after appending a partial final JSON row
                # but before the index fsync.  Keep the valid prefix readable;
                # a complete malformed line is still treated as corruption.
                with open(jsonl_path, "rb") as source:
                    source.seek(last_end)
                    trailing = source.read()
                incomplete_tail = bool(trailing) and b"\n" not in trailing
                if not incomplete_tail:
                    # A malformed final line (or a newline-only torn write)
                    # can be discarded safely because it is not represented
                    # by the index.  A valid JSON object after the indexed
                    # prefix means the index is genuinely stale and must be
                    # rebuilt instead.
                    trailing_has_valid_row = False
                    for raw in trailing.splitlines():
                        if not raw.strip():
                            continue
                        try:
                            candidate = json.loads(raw.decode("utf-8"))
                        except (UnicodeDecodeError, TypeError, ValueError):
                            continue
                        if isinstance(candidate, dict):
                            trailing_has_valid_row = True
                            break
                    if trailing_has_valid_row:
                        raise ValueError("history index is stale")
                    incomplete_tail = True
                # Remove only the unindexed partial tail so the next append
                # starts at a valid line boundary.
                with open(jsonl_path, "r+b") as source:
                    source.truncate(last_end)
                    source.flush()
                    os.fsync(source.fileno())
                file_size = last_end
            if not entries and file_size and not incomplete_tail:
                raise ValueError("history index is empty")
            return entries
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            if not rebuild_on_error:
                raise
            self._rebuild_history_index_unlocked(jsonl_path, index_path)
            return self._load_history_index_unlocked(jsonl_path, index_path, rebuild_on_error=False)

    def _read_history_page_from_index_unlocked(
        self, jsonl_path: str, entries: list[dict[str, Any]], start: int, limit: int
    ) -> list[dict[str, Any]]:
        if not entries or start >= len(entries):
            return []
        page: list[dict[str, Any]] = []
        with open(jsonl_path, "rb") as stream:
            for index in range(start, min(len(entries), start + limit)):
                expected = entries[index]
                stream.seek(max(0, int(expected.get("offset", 0))))
                raw = stream.readline()
                if not raw:
                    raise ValueError("history JSONL is shorter than its index")
                if stream.tell() != expected.get("end"):
                    raise ValueError("history index end mismatch")
                try:
                    item = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, TypeError, ValueError) as exc:
                    raise ValueError("history JSONL contains an invalid row") from exc
                if not isinstance(item, dict) or item.get("id", "") != expected.get("id", ""):
                    raise ValueError("history index row mismatch")
                page.append(item)
        return page

    def _migrate_history_to_jsonl_unlocked(self, bot_id: str, user_id: str | None = None) -> str:
        """Create the append-only history beside a legacy JSON snapshot.

        The migration is deliberately additive: the old JSON file remains as a
        rollback/compatibility snapshot, while all subsequent appends go to
        JSONL.  It runs under ``self._lock`` and only once per runtime path.
        """
        jsonl_path = self._history_jsonl_path(bot_id, user_id)
        if os.path.exists(jsonl_path):
            return jsonl_path
        legacy_path = self._history_path(bot_id, user_id)
        if not os.path.isfile(legacy_path):
            return jsonl_path
        try:
            with open(legacy_path, "r", encoding="utf-8") as stream:
                history = json.load(stream)
            if not isinstance(history, list):
                return jsonl_path
            parent = os.path.dirname(jsonl_path)
            os.makedirs(parent, mode=0o700, exist_ok=True)
            tmp_path = f"{jsonl_path}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as output:
                for item in history:
                    if isinstance(item, dict):
                        output.write(json.dumps(item, ensure_ascii=False, default=str, separators=(",", ":")))
                        output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, jsonl_path)
            self._rebuild_history_index_unlocked(jsonl_path)
        except (OSError, TypeError, ValueError):
            with suppress(OSError):
                os.remove(locals().get("tmp_path", ""))
        return jsonl_path

    def _compact_history_jsonl_unlocked(self, jsonl_path: str) -> None:
        """Compact an oversized JSONL file while retaining recent messages."""
        try:
            recent: deque[dict[str, Any]] = deque(maxlen=5000)
            total_bytes = 0
            with open(jsonl_path, "r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(item, dict):
                        recent.append(item)
            # Calculate from newest to oldest so the retained tail fits the
            # same byte budget as the legacy JSON implementation.
            retained: list[dict[str, Any]] = []
            total_bytes = 0
            for item in reversed(recent):
                size = _estimate_history_entry_bytes(item)
                if retained and total_bytes + size > MAX_HISTORY_TOTAL_BYTES:
                    break
                retained.append(item)
                total_bytes += size
            retained.reverse()
            tmp_path = f"{jsonl_path}.{os.getpid()}.{threading.get_ident()}.compact.tmp"
            with open(tmp_path, "w", encoding="utf-8") as output:
                for item in retained:
                    output.write(json.dumps(item, ensure_ascii=False, default=str, separators=(",", ":")))
                    output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, jsonl_path)
            self._rebuild_history_index_unlocked(jsonl_path)
        except (OSError, TypeError, ValueError):
            with suppress(OSError):
                os.remove(locals().get("tmp_path", ""))

    def _replace_history_jsonl_unlocked(self, jsonl_path: str, history: list[dict[str, Any]]) -> None:
        tmp_path = f"{jsonl_path}.{os.getpid()}.{threading.get_ident()}.replace.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as output:
                for item in history:
                    output.write(json.dumps(item, ensure_ascii=False, default=str, separators=(",", ":")))
                    output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, jsonl_path)
            self._rebuild_history_index_unlocked(jsonl_path)
        except Exception:
            with suppress(OSError):
                os.remove(tmp_path)
            raise

    def _read_history_jsonl_unlocked(
        self,
        jsonl_path: str,
        limit: int,
        before_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            entries = self._load_history_index_unlocked(jsonl_path)
        except (OSError, ValueError, TypeError):
            return {"history": [], "total": 0, "has_more": False}
        total = len(entries)
        if not total:
            return {"history": [], "total": 0, "has_more": False}

        page_limit = max(1, int(limit))
        if before_id is None:
            target = total
        else:
            target = next((idx for idx, item in enumerate(entries) if item.get("id") == before_id), -1)
            if target < 0:
                return {"history": [], "total": total, "has_more": False}
        start = max(0, target - page_limit)
        try:
            page = self._read_history_page_from_index_unlocked(jsonl_path, entries, start, target - start)
        except (OSError, ValueError, TypeError):
            # A crash or manual edit can leave a same-sized but stale index.
            # Rebuild once and retry the requested page from the repaired map.
            try:
                self._rebuild_history_index_unlocked(jsonl_path)
                entries = self._load_history_index_unlocked(jsonl_path)
                total = len(entries)
                if before_id is None:
                    target = total
                else:
                    target = next((idx for idx, item in enumerate(entries) if item.get("id") == before_id), -1)
                    if target < 0:
                        return {"history": [], "total": total, "has_more": False}
                start = max(0, target - page_limit)
                page = self._read_history_page_from_index_unlocked(jsonl_path, entries, start, target - start)
            except (OSError, ValueError, TypeError):
                return {"history": [], "total": 0, "has_more": False}
        return {"history": page, "total": total, "has_more": start > 0}

    def _uploads_dir(self, bot_id: str, user_id: str | None = None) -> str:
        return os.path.join(self._runtime_dir(bot_id, user_id), "uploads")

    def ensure_legacy_state(self, bot_id: str, user_id: str | None = None) -> None:
        """Lazily inherit pre-auth public BOT state into the user's partition.

        Before authentication was enabled, public BOT history and uploaded files
        lived directly under the BOT directory.  Authenticated requests use an
        isolated ``users/<hash>`` directory, so copy the legacy state on first
        access.  The legacy directory is never used as a live fallback and is
        left intact, making this operation idempotent and preserving local-mode
        compatibility.
        """
        bot = self.get_bot(bot_id)
        effective_user_id = user_id or runtime_user_id.get()
        if not bot or bot.visibility != "public" or effective_user_id == "local":
            return

        bot_dir = self._bot_dir(bot_id)
        partition_dir = self._runtime_dir(bot_id, effective_user_id)
        if partition_dir == bot_dir:
            return

        legacy_history = os.path.join(bot_dir, "history.json")
        target_history = os.path.join(partition_dir, "history.json")
        legacy_memory = os.path.join(bot_dir, "memory.json")
        target_memory = os.path.join(partition_dir, "memory.json")
        legacy_uploads = os.path.join(bot_dir, "uploads")
        target_uploads = os.path.join(partition_dir, "uploads")
        migration_marker = os.path.join(partition_dir, ".legacy_state_migrated")

        with self._lock:
            if os.path.exists(migration_marker):
                return
            legacy_state_exists = (
                os.path.isfile(legacy_history)
                or os.path.isfile(legacy_memory)
                or os.path.isdir(legacy_uploads)
            )
            if not legacy_state_exists:
                return

            # A target history file is treated as an explicit user state (even
            # when it contains []).  Never overwrite or merge it with legacy
            # messages after that point.
            if os.path.isfile(legacy_history) and not os.path.exists(target_history):
                try:
                    with open(legacy_history, "r", encoding="utf-8") as f:
                        history = json.load(f)
                    if isinstance(history, list):
                        bounded_history = []
                        for item in history[-5000:]:
                            if not isinstance(item, dict):
                                continue
                            bounded = dict(item)
                            bounded["content"] = _truncate_history_text(bounded.get("content"))
                            bounded["metadata"] = _bound_history_metadata(bounded.get("metadata"))
                            bounded_history.append(bounded)
                        _atomic_dump_json(target_history, _trim_history_to_budget(bounded_history), default=str)
                except (OSError, ValueError, TypeError):
                    # A corrupt legacy file must not make an authenticated BOT
                    # unusable; the normal empty-history path will recover.
                    pass

            # Long-term facts were also stored in the legacy runtime directory.
            # Preserve a valid snapshot for each user without merging later
            # user-specific updates into another user's partition.
            if os.path.isfile(legacy_memory) and not os.path.exists(target_memory):
                try:
                    with open(legacy_memory, "r", encoding="utf-8") as f:
                        memories = json.load(f)
                    if isinstance(memories, list):
                        _atomic_dump_json(target_memory, memories, default=str)
                except (OSError, ValueError, TypeError):
                    pass

            if os.path.isdir(legacy_uploads):
                try:
                    os.makedirs(target_uploads, exist_ok=True)
                    copied_count = 0
                    copied_bytes = 0
                    for filename in os.listdir(legacy_uploads):
                        source = os.path.join(legacy_uploads, filename)
                        target = os.path.join(target_uploads, filename)
                        # Legacy files are local persistence, but skip links and
                        # directories so migration cannot copy outside the BOT.
                        if os.path.islink(source) or not os.path.isfile(source):
                            continue
                        if os.path.exists(target):
                            continue
                        try:
                            source_size = os.path.getsize(source)
                        except OSError:
                            continue
                        if copied_count >= MAX_RUNTIME_FILES or copied_bytes + source_size > MAX_RUNTIME_TOTAL_FILE_BYTES:
                            break
                        try:
                            shutil.copy2(source, target)
                            copied_count += 1
                            copied_bytes += source_size
                        except OSError:
                            continue
                except OSError:
                    pass

            # The marker prevents a user who explicitly deleted an inherited
            # file/history from seeing it resurrected on the next request.
            try:
                _atomic_dump_json(migration_marker, {"version": 1}, default=str)
            except OSError:
                pass

    def _ensure_default_bot(self):
        default_id = "default_assistant"
        path = self._bot_dir(default_id)
        default_name = "腾讯云全能架构管家"
        default_desc = "内置腾讯云多业务云产品标准库（云直播 CSS、即时通信 IM 及未来扩展云服务）与 Python 执行沙箱的智能调度架构管家"
        default_prompt = (
            "你是一个专业的腾讯云多业务云架构与自动化运维技术专家（涵盖云直播 API 3.0、即时通信 IM REST API，并具备即插即用扩展任意云服务的能力）。\n"
            "你精通腾讯云 API 3.0 规范（Action、签名、通用混流、流状态巡检、录制转码）、IM 架构（UserSig、REST、群组关系链、消息流转）。\n"
            "你可以通过直接编写并执行 Python 脚本，调度 `cloud_call`、`live_call`、`im_call`、`cloud_search_apis` 等工具完成接口联调、复杂排障、多流监控与批量数据处理。\n"
            "当发现用户有高频或特定模式的需求时，你可以主动通过工具将经验固化为【技能】或【强化自己的提示词】。"
        )
        default_prompts = [
            "查询当前在线直播流推流状态并统计码率与帧率",
            "拉取违规直播流并调用 DropLiveStream 执行安全熔断断流",
            "配置直播通用混流模板 (CreateCommonMixStream) 并输出参数结构",
            "批量拉取活跃群组最近 20 条消息并导出 Excel 分析报表"
        ]

        if not os.path.exists(path):
            self.create_bot(
                bot_id=default_id,
                name=default_name,
                avatar="🤖",
                description=default_desc,
                base_prompt=default_prompt,
                suggested_prompts=default_prompts,
                visibility="public",
            )
        else:
            # 升级已有 default_assistant 的描述与人设，平滑扩展多云业务，保留已有历史与记忆
            bot_json = os.path.join(path, "bot.json")
            if os.path.exists(bot_json):
                try:
                    with open(bot_json, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    data["visibility"] = "public"
                    if "166" in data.get("description", "") or "261" in data.get("description", "") or "IM (Instant Messaging)" in data.get("base_prompt", ""):
                        data["name"] = default_name
                        data["description"] = default_desc
                        data["base_prompt"] = default_prompt
                        data["suggested_prompts"] = default_prompts
                    _atomic_dump_json(bot_json, data)
                except Exception:
                    pass

        # 确保云直播专属巡检官内置就绪
        live_id = "css_live_operator"
        live_path = self._bot_dir(live_id)
        if not os.path.exists(live_path):
            self.create_bot(
                bot_id=live_id,
                name="云直播流媒体巡检官",
                avatar="📡",
                description="专精腾讯云直播 CSS 流状态巡检、混流布局配置、断流禁推熔断与音视频推拉流诊断",
                base_prompt=(
                    "你是一个专精腾讯云直播 (CSS) 核心业务与流媒体运维的专业智能体。\n"
                    "你熟练掌握腾讯云直播 API 3.0 体系（包括流状态查询、推断流监控、录制转码规则、混流模版、鉴权防盗链及安全熔断）。\n"
                    "在处理推断流与混流任务时，编写紧凑高效的 Python 代码直接调用 `live_call(action, payload)` 或 `cloud_call`，提取 Response 中的核心流媒体指标。\n"
                    "对涉及断流 (DropLiveStream)、封禁推流 (ForbidLiveStream) 的操作，严格遵守防线原则，确保流名、域名与解禁时间精确无误。"
                ),
                suggested_prompts=[
                    "巡检指定域名的所有在线直播流及其实时帧率",
                    "对指定的异常推流执行 DropLiveStream 立即断流",
                    "生成一套主辅画面画中画的通用混流参数 (CreateCommonMixStream)",
                    "导出本月直播录制与转码规则配置清单"
                ],
                visibility="public",
            )

    def list_bots(self, user_id: str | None = None) -> list[dict[str, Any]]:
        result = []
        if not os.path.exists(self.bots_dir):
            return result

        for entry in os.listdir(self.bots_dir):  # pi-lens-ignore: unchecked-throwing-call-python -- persistence boundary
            bot_path = os.path.join(self.bots_dir, entry)
            bot_json = os.path.join(bot_path, "bot.json")
            if os.path.isdir(bot_path) and os.path.exists(bot_json):
                try:
                    with open(bot_json, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        # 统计资产数量
                        self.ensure_legacy_state(entry, user_id)
                        runtime_dir = self._runtime_dir(entry, user_id)
                        skills_dir = os.path.join(bot_path, "skills")
                        skills_count = len([f for f in os.listdir(skills_dir) if f.endswith(".json")]) if os.path.exists(skills_dir) else 0
                        mem_path = os.path.join(runtime_dir, "memory.json")
                        mem_count = 0
                        if os.path.exists(mem_path):
                            with open(mem_path, "r", encoding="utf-8") as mf:
                                mem_count = len(json.load(mf))
                        data["skills_count"] = skills_count
                        data["memories_count"] = mem_count
                        data["guidelines_count"] = len(data.get("learned_guidelines", []))

                        uploads_dir = os.path.join(runtime_dir, "uploads")
                        files_count = len([f for f in os.listdir(uploads_dir) if os.path.isfile(os.path.join(uploads_dir, f))]) if os.path.exists(uploads_dir) else 0
                        data["files_count"] = files_count

                        # 读取最近一条聊天记录 (微信/WhatsApp 风格会话列表摘要)
                        last_message = None
                        hist = self.get_history(entry, limit=1, user_id=user_id)
                        if hist:
                            last_item = hist[-1]
                            last_message = {
                                "role": last_item.get("role", "assistant"),
                                "content": last_item.get("content", ""),
                                "timestamp": last_item.get("timestamp", "")
                            }
                        data["last_message"] = last_message
                        result.append(data)
                except Exception:
                    continue

        # 排序：默认智能体置顶，其余按最后聊天/更新时间倒序
        def get_sort_key(b):
            is_default = 1 if b.get("id") == "default_assistant" else 0
            last_ts = ""
            if b.get("last_message") and b["last_message"].get("timestamp"):
                last_ts = b["last_message"]["timestamp"]
            elif b.get("updated_at"):
                last_ts = b["updated_at"]
            elif b.get("created_at"):
                last_ts = b["created_at"]
            return (is_default, last_ts)

        result.sort(key=get_sort_key, reverse=True)
        return result

    @staticmethod
    def clean_prompt_text(text: str) -> str:
        """清洗由于对象直接转字符串而残留的 ChatMessage(...) 包装体"""
        if not text:
            return ""
        s = text.strip()
        if s.startswith("ChatMessage(") and "content=" in s:
            m = re.search(r"content=(['\"])(.*?)\1(?:,\s*(?:tool_calls|raw|refusal|role|annotations))", s, re.DOTALL)
            if m:
                extracted = m.group(2)
                if "\\u" in extracted:
                    with suppress(Exception):
                        extracted = extracted.encode("latin1").decode("unicode_escape")
                return extracted.strip()
        return s

    @staticmethod
    def infer_suggested_prompts(name: str, goal: str = "", base_prompt: str = "") -> list[str]:
        """根据智能体名称、目标与人格描述，智能生成 4 条具体可执行的专属业务指令/问题"""
        primary_text = f"{name} {goal}".lower()
        if "全能" in name or "default_assistant" in primary_text:
            return [
                "为我新建一个【社群风控官】智能体，负责敏感词检测与一键禁言",
                "帮我新建一个【IM运维巡检官】智能体，专注排查高频报错与离线",
                "结合腾讯云全量 API，为我分析当前系统核心接口清单",
                "列出所有已入驻的智能体，并指挥相关智能体开展协作"
            ]
        if any(k in primary_text for k in ["直播", "推流", "断流", "流媒体", "转码", "录制", "导播", "数字人", "live", "stream", "css"]):
            return [
                "批量巡检在线直播流状态，筛选出推流码率或帧率异常的直播间",
                "生成一个测试房间的推流地址与带防盗链签名的播放链接",
                "对违规直播流执行紧急断流操作并查询断流历史记录",
                "查询当前已配置的转码模板与录制规则清单"
            ]
        if any(k in primary_text for k in ["投研", "投资", "研报", "金融", "证券"]):
            return [
                "帮我排查投研群研报推送失败的具体原因",
                "批量把今日宏观研报分发到指定的三个投研群",
                "检查投研群成员权限与认证投顾名单匹配状态",
                "整理并归档本周研报分发记录并标出异常链路"
            ]
        if any(k in primary_text for k in ["巡检", "监控", "健康", "诊断", "丢包", "延时", "inspector", "monitor"]):
            return [
                "巡检今日重点大群消息流，标记丢包与延时异常并出报告",
                "扫描金融大群近一小时消息，列出延时超过阈值的会话",
                "排查重点群今日高峰期消息丢包原因并输出定位结论",
                "对比重点大群昨日与今日同时段消息到达率，标注异常"
            ]
        if any(k in primary_text for k in ["风控", "安全", "拦截", "黑名单", "防护", "违规", "审核", "security", "risk"]):
            return [
                "检索全部与禁言和拉黑相关的 REST 接口与调用字段",
                "检查近期高频违规发言账号并批量加入全局黑名单",
                "编写脚本扫描活跃大群的敏感词拦截与风控记录",
                "请记住：以后的群禁言操作，默认都加上 3600 秒时长"
            ]
        if any(k in primary_text for k in ["消息", "漫游", "撤回", "msg", "chat", "message"]):
            return [
                "帮我排查用户单聊与群聊消息发送失败的具体原因",
                "核查某用户漫游消息记录缺失的接口同步链路",
                "编写脚本批量撤回指定群中违规或测试发送的消息",
                "配置 IM 消息撤回时效与管理员审计回调规范"
            ]
        if any(k in primary_text for k in ["数据", "统计", "报表", "对账", "分析", "活跃", "data", "stats", "metrics"]):
            return [
                "按天统计近 30 天群聊活跃度与消息总吞吐量",
                "分析近期进群、退群异常波动的群组排名与归因",
                "编写 Python 脚本拉取并清洗管理员消息审计数据",
                "输出当前最活跃的 Top 10 群组与成员分布清单"
            ]
        if any(k in primary_text for k in ["群", "社群", "万人群", "讨论组", "成员", "group"]):
            return [
                "查看当前有哪些群组，并列出人数最多、最活跃的群 ID",
                "编写脚本批量查询群主离线超过 30 天的静默群",
                "调用群组接口批量拉取指定万人群的成员列表与禁言状态",
                "将经常使用的核心测试群 ID 记录在长期记忆中"
            ]
        if any(k in primary_text for k in ["运维", "压测", "性能", "系统", "高可用", "ops", "perf"]):
            return [
                "编写压测脚本并发测试消息下发与回调响应延迟",
                "批量拉取所有群基础资料并导出为规范报表",
                "检查当前 AppKey 与管理员签名 UserSig 是否有效",
                "将高频运维排查脚本保存为专属技能以备后续调用"
            ]
        if any(k in primary_text for k in ["测试", "验证", "自动化", "回归", "test", "qa"]):
            return [
                "编写自动化脚本验证单聊、群聊消息发送的连通性",
                "批量注册 10 个虚拟测试账号并加入测试群组",
                "模拟网络异常与接口超时并验证容错机制",
                "保存常用的测试用例流程为一个自动化执行技能"
            ]
        # 通用兜底
        return [
            f"结合腾讯云多业务全量 API，为我分析【{name}】的核心接口清单",
            "编写 Python 脚本批量检查关键账号的在线与禁言状态",
            "列出当前系统中所有的群组并按成员规模排序",
            "将我常用的测试配置与参数记在你的长期记忆中"
        ]

    def generate_bot_suggested_prompts(self, bot_id: str, force_refresh: bool = False) -> list[str]:
        """为指定智能体使用大模型深度生成 4 条个性化、贴合其人设定位的业务指令"""
        bot = self.get_bot(bot_id)
        if not bot:
            return []

        if not force_refresh and bot.suggested_prompts and len(bot.suggested_prompts) >= 3:
            return bot.suggested_prompts

        # 尝试通过大模型生成
        llm_cfg = self.get_llm_config()
        if (llm_cfg.api_key or llm_cfg.provider == "ollama") and llm_cfg.model_name and not os.environ.get("PYTEST_CURRENT_TEST"):
            try:
                from smolagents import OpenAIServerModel
                api_base = llm_cfg.base_url.rstrip("/") if llm_cfg.base_url else None
                if llm_cfg.provider == "ollama" and api_base and not api_base.endswith("/v1"):
                    api_base = f"{api_base}/v1"
                if api_base:
                    from core.agent.model_url_guard import validate_model_base_url

                    api_base = validate_model_base_url(api_base)
                model = OpenAIServerModel(
                    model_id=llm_cfg.model_name,
                    api_base=api_base,
                    api_key=llm_cfg.api_key or "ollama",
                    temperature=0.4
                )
                system_task = (
                    f"你是一个智能体推荐指令生成专家。\n"
                    f"当前智能体：【{bot.name}】\n"
                    f"职责定位与人设：【{bot.description or bot.base_prompt[:120]}】\n"
                    f"请根据该智能体的人设定位与职责范围，生成 4 条用户最可能对它提出的典型业务指令或高价值问题。"
                    f"要求：紧扣该智能体的人格特征与专业职责，指令具体可执行，每条15~30字，不要泛泛而谈，具有明确的代入感与实用性。\n"
                    f"请仅以 JSON 数组格式返回，例如 [\"指令1\", \"指令2\", \"指令3\", \"指令4\"]，禁止包含 markdown 代码块或任何其他内容。"
                )
                resp = model([{"role": "user", "content": system_task}])
                raw_text = ""
                if hasattr(resp, "content") and resp.content:
                    raw_text = str(resp.content).strip()
                else:
                    response_text = getattr(resp, "text", None)
                    raw_text = str(response_text).strip() if response_text else str(resp).strip()

                if "```json" in raw_text:
                    raw_text = raw_text.split("```json")[1].split("```")[0].strip()
                elif "```" in raw_text:
                    raw_text = raw_text.split("```")[1].split("```")[0].strip()

                parsed = json.loads(raw_text)
                if isinstance(parsed, list) and len(parsed) >= 2:
                    prompts = [str(p).strip() for p in parsed if str(p).strip()][:4]
                    bot.suggested_prompts = prompts
                    self.save_bot(bot)
                    return prompts
            except Exception:
                pass

        # 规则推断兜底
        fallback_prompts = self.infer_suggested_prompts(bot.name, bot.description, bot.base_prompt)
        bot.suggested_prompts = fallback_prompts
        self.save_bot(bot)
        return fallback_prompts

    def get_bot(self, bot_id: str) -> BotConfig | None:
        bot_json = os.path.join(self._bot_dir(bot_id), "bot.json")
        if not os.path.exists(bot_json):
            return None
        try:
            with open(bot_json, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "base_prompt" in data:
                cleaned = self.clean_prompt_text(data["base_prompt"])
                if cleaned != data["base_prompt"]:
                    data["base_prompt"] = cleaned
            bot = BotConfig(**data)
            if not bot.suggested_prompts:
                bot.suggested_prompts = self.infer_suggested_prompts(bot.name, bot.description, bot.base_prompt)
            return bot
        except Exception:
            return None

    def create_bot(
        self,
        bot_id: str,
        name: str,
        avatar: str = "🤖",
        description: str = "",
        base_prompt: str | None = None,
        suggested_prompts: list[str] | None = None,
        model_override: dict | None = None,
        owner_id: str | None = None,
        visibility: str = "private",
    ) -> BotConfig:
        b_dir = self._bot_dir(bot_id)
        os.makedirs(b_dir, exist_ok=True)  # pi-lens-ignore: unchecked-throwing-call-python -- persistence boundary
        os.makedirs(os.path.join(b_dir, "skills"), exist_ok=True)  # pi-lens-ignore: unchecked-throwing-call-python -- persistence boundary

        cleaned_base_prompt = self.clean_prompt_text(base_prompt or "你是一个专业的腾讯云 IM 智能助手。")
        if not suggested_prompts:
            suggested_prompts = self.infer_suggested_prompts(name, description, cleaned_base_prompt)

        bot = BotConfig(
            id=bot_id,
            name=name,
            avatar=avatar,
            description=description,
            base_prompt=cleaned_base_prompt,
            suggested_prompts=suggested_prompts,
            model_override=model_override,
            owner_id=owner_id,
            visibility=visibility,
        )
        self.save_bot(bot)
        # 初始化空记忆与历史
        mem_path = os.path.join(b_dir, "memory.json")
        if not os.path.exists(mem_path):
            _atomic_dump_json(mem_path, [])
        hist_path = os.path.join(b_dir, "history.json")
        if not os.path.exists(hist_path):
            _atomic_dump_json(hist_path, [])
        return bot

    def save_bot(self, bot: BotConfig):
        with self._lock:
            b_dir = self._bot_dir(bot.id)
            os.makedirs(b_dir, exist_ok=True)  # pi-lens-ignore: unchecked-throwing-call-python -- persistence boundary
            bot.updated_at = now_iso()
            _atomic_dump_json(os.path.join(b_dir, "bot.json"), bot.model_dump())

    def delete_bot(self, bot_id: str) -> bool:
        if bot_id == "default_assistant":
            return False  # 保护默认 BOT
        with self._lock:
            b_dir = self._bot_dir(bot_id)
            if os.path.exists(b_dir):
                shutil.rmtree(b_dir)  # pi-lens-ignore: unchecked-throwing-call-python -- persistence boundary
                try:
                    from .engine import agent_engine
                    agent_engine.clear_bot_sandboxes(bot_id)
                except Exception:
                    pass
                return True
            return False

    def generate_bot_profile(self, name: str, goal: str = "") -> dict[str, Any]:
        """根据用户输入的 BOT 名字和目标，智能生成 BOT ID、图标和专业提示词"""
        clean_name = name.strip() or "自定义助手"
        clean_goal = goal.strip() or f"协助管理与排查【{clean_name}】相关的腾讯云业务需求"

        # 1. 智能推断 Emoji 头像
        avatar = "🤖"
        kw_map = [
            (["直播", "推流", "拉流", "流媒体", "转码", "录制", "导播", "数字人", "视频", "live", "stream"], "🎥"),
            (["风控", "安全", "拦截", "黑名单", "防护", "违规", "审核"], "🛡️"),
            (["巡检", "排查", "监控", "检测", "诊断", "健康"], "🔍"),
            (["群", "社群", "讨论组", "万人群", "成员"], "👥"),
            (["消息", "聊天", "对话", "通知", "推送", "广播", "撤回"], "💬"),
            (["数据", "分析", "统计", "报表", "对账", "活跃"], "📊"),
            (["运维", "压测", "性能", "系统", "高可用"], "⚡"),
            (["测试", "验证", "自动化", "回归"], "🧪"),
            (["客服", "答疑", "服务", "接待"], "🎧"),
            (["文档", "知识", "帮助", "规范"], "📚"),
        ]
        combined_text = f"{clean_name} {clean_goal}".lower()
        for keywords, emoji in kw_map:
            if any(k in combined_text for k in keywords):
                avatar = emoji
                break

        # 2. 生成合法的英文字符 ID
        timestamp_suffix = str(int(time.time()))[-4:]
        # 英文词根提取
        slug_prefix = "bot"
        if any(k in combined_text for k in ["直播", "推流", "拉流", "live", "stream", "css"]):
            slug_prefix = "live"
        elif any(k in combined_text for k in ["风控", "安全", "security", "risk"]):
            slug_prefix = "security"
        elif any(k in combined_text for k in ["巡检", "监控", "inspector", "monitor"]):
            slug_prefix = "inspector"
        elif any(k in combined_text for k in ["群", "group"]):
            slug_prefix = "group"
        elif any(k in combined_text for k in ["消息", "msg", "message"]):
            slug_prefix = "msg"
        elif any(k in combined_text for k in ["数据", "统计", "data", "stats"]):
            slug_prefix = "data"
        elif any(k in combined_text for k in ["运维", "ops"]):
            slug_prefix = "ops"
        elif any(k in combined_text for k in ["测试", "test"]):
            slug_prefix = "test"
        auto_id = f"bot_{slug_prefix}_{timestamp_suffix}"

        # 3. 生成专业精准的 System Prompt
        base_prompt = (
            f"你是一个专注于【{clean_name}】的腾讯云多业务专属智能体助手。\n\n"
            f"【核心定位与使命】\n"
            f"{clean_goal}\n\n"
            f"【专属工作准则与规范】\n"
            f"1. 你完全掌控腾讯云多业务全量接口（涵盖云直播 API 3.0、即时通信 IM REST 等）的最高调度权限（`cloud_call`、`live_call`、`im_call`），以及完整的官方文档全文索引与语义检索能力。\n"
            f"2. 当接到用户的业务指令时，优先针对【{clean_name}】的核心职责进行深度的 API 调用与 Python 脚本编排，支持循环、批处理与数据清洗。\n"
            f"3. 保持严谨客观，在执行涉及批量封禁、剔除或修改的操作前，向用户明确汇总受影响的对象范围与执行参数。\n"
            f"4. 遇到高复用价值的自动化流程或事实记忆，在后台自主调用 `save_skill` 或 `save_memory` 静默沉淀，保持回复精简专业，无需打扰用户。\n"
            f"5. 积极倾听用户对你的长期习惯与约束要求，适时调用 `enhance_self_prompt` 强化自己的准则。"
        )

        # 4. 生成个性化业务建议指令
        suggested_prompts = self.infer_suggested_prompts(clean_name, clean_goal, base_prompt)

        # 5. 尝试通过大模型进行更加深度的丰富（若已配置大模型且非单测环境）
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return {
                "id": auto_id,
                "name": clean_name,
                "avatar": avatar,
                "description": clean_goal[:80],
                "base_prompt": base_prompt,
                "suggested_prompts": suggested_prompts
            }

        llm_cfg = self.get_llm_config()
        if (llm_cfg.api_key or llm_cfg.provider == "ollama") and llm_cfg.model_name:
            try:
                from smolagents import OpenAIServerModel
                api_base = llm_cfg.base_url.rstrip("/") if llm_cfg.base_url else None
                if llm_cfg.provider == "ollama" and api_base and not api_base.endswith("/v1"):
                    api_base = f"{api_base}/v1"
                if api_base:
                    from core.agent.model_url_guard import validate_model_base_url

                    api_base = validate_model_base_url(api_base)
                model = OpenAIServerModel(
                    model_id=llm_cfg.model_name,
                    api_base=api_base,
                    api_key=llm_cfg.api_key or "ollama",
                    temperature=0.3
                )
                system_task = (
                    f"用户正在腾讯云多业务控制台 Studio 中创建名为【{clean_name}】的专属智能体 BOT。\n"
                    f"用户的职责目标设定是：【{clean_goal}】。\n"
                    f"请以 JSON 格式输出，不要包含任何 markdown 代码块标识（纯 JSON 文本）：\n"
                    f'{{\n'
                    f'  "base_prompt": "约 200 字专属 System Prompt 正文，体现专业严谨人设，结合腾讯云多业务全量 API、Python 沙箱执行能力、自主技能沉淀与准则自我增强机制",\n'
                    f'  "suggested_prompts": ["4条用户最可能对该智能体提出的典型业务指令或问题，紧扣其人设定位，每条15~35字"]\n'
                    f'}}\n'
                )
                resp = model([{"role": "user", "content": system_task}])
                # 关键修复：从 smolagents ChatMessage 对象提取 content 字符串，绝不可直接 str(resp)！
                raw_text = ""
                if hasattr(resp, "content") and resp.content:
                    raw_text = str(resp.content).strip()
                else:
                    response_text = getattr(resp, "text", None)
                    if response_text:
                        raw_text = str(response_text).strip()
                    elif isinstance(resp, str):
                        raw_text = resp.strip()
                    elif isinstance(resp, dict):
                        raw_text = str(resp.get("content") or resp.get("text") or "").strip()
                    else:
                        raw_text = str(resp).strip()

                raw_text = self.clean_prompt_text(raw_text)

                # 解析 JSON 返回
                json_str = raw_text
                if "```json" in json_str:
                    json_str = json_str.split("```json")[1].split("```")[0].strip()
                elif "```" in json_str:
                    json_str = json_str.split("```")[1].split("```")[0].strip()

                try:
                    parsed = json.loads(json_str)
                    if isinstance(parsed, dict):
                        if parsed.get("base_prompt") and len(str(parsed["base_prompt"]).strip()) > 50:
                            base_prompt = self.clean_prompt_text(str(parsed["base_prompt"]).strip())
                        if parsed.get("suggested_prompts") and isinstance(parsed["suggested_prompts"], list) and len(parsed["suggested_prompts"]) > 0:
                            suggested_prompts = [str(p).strip() for p in parsed["suggested_prompts"] if str(p).strip()][:5]
                except Exception:
                    if len(raw_text) > 50 and not raw_text.startswith("{"):
                        base_prompt = raw_text
            except Exception:
                pass  # 优雅降级使用内置的高标准模板与规则推断

        return {
            "id": auto_id,
            "name": clean_name,
            "avatar": avatar,
            "description": clean_goal[:80],
            "base_prompt": self.clean_prompt_text(base_prompt),
            "suggested_prompts": suggested_prompts
        }

    # -------------------------------------------------------------
    # 提示词自我增强 (Prompt Self-Enhancement)
    # -------------------------------------------------------------
    def add_learned_guideline(self, bot_id: str, instruction: str, reason: str = "") -> LearnedGuideline | None:
        bot = self.get_bot(bot_id)
        if not bot:
            return None
        guideline = LearnedGuideline(
            id=f"g-{int(time.time() * 1000)}",
            instruction=instruction.strip(),
            reason=reason.strip()
        )
        bot.learned_guidelines.append(guideline)
        self.save_bot(bot)
        return guideline

    def remove_learned_guideline(self, bot_id: str, guideline_id: str) -> bool:
        bot = self.get_bot(bot_id)
        if not bot:
            return False
        orig_len = len(bot.learned_guidelines)
        bot.learned_guidelines = [g for g in bot.learned_guidelines if g.id != guideline_id]
        if len(bot.learned_guidelines) < orig_len:
            self.save_bot(bot)
            return True
        return False

    # -------------------------------------------------------------
    # 长期事实记忆系统 (Memory System)
    # -------------------------------------------------------------
    def list_memories(self, bot_id: str) -> list[MemoryItem]:
        # Ensure pre-auth public BOT memory is inherited before the first read.
        # This keeps the memory panel consistent with history/files migration.
        self.ensure_legacy_state(bot_id)
        mem_path = os.path.join(self._runtime_dir(bot_id), "memory.json")
        if not os.path.exists(mem_path):
            return []
        try:
            with open(mem_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
                return [MemoryItem(**item) for item in raw]
        except Exception:
            return []

    def save_memory(self, bot_id: str, key: str, content: str) -> MemoryItem:
        with self._lock:
            memories = self.list_memories(bot_id)
            now_str = now_iso()
            found = False
            target_item: MemoryItem | None = None
            for m in memories:
                if m.key == key:
                    m.content = content
                    m.updated_at = now_str
                    target_item = m
                    found = True
                    break
            if not found:
                target_item = MemoryItem(key=key, content=content, created_at=now_str, updated_at=now_str)
                memories.append(target_item)

            mem_path = os.path.join(self._runtime_dir(bot_id), "memory.json")
            _atomic_dump_json(mem_path, [m.model_dump() for m in memories])
            if target_item is None:
                raise RuntimeError("memory item was not created")
            return target_item

    def delete_memory(self, bot_id: str, key: str) -> bool:
        with self._lock:
            memories = self.list_memories(bot_id)
            filtered = [m for m in memories if m.key != key]
            if len(filtered) < len(memories):
                mem_path = os.path.join(self._runtime_dir(bot_id), "memory.json")
                _atomic_dump_json(mem_path, [m.model_dump() for m in filtered])
                return True
            return False

    # -------------------------------------------------------------
    # 技能自沉淀系统 (Self-Evolving Skills)
    # -------------------------------------------------------------
    def _skills_dir(self, bot_id: str) -> str:
        s_dir = os.path.join(self._bot_dir(bot_id), "skills")
        os.makedirs(s_dir, exist_ok=True)  # pi-lens-ignore: unchecked-throwing-call-python -- persistence boundary
        return s_dir

    def list_skills(self, bot_id: str) -> list[SkillItem]:
        s_dir = self._skills_dir(bot_id)
        result = []
        for file in os.listdir(s_dir):  # pi-lens-ignore: unchecked-throwing-call-python -- persistence boundary
            if file.endswith(".json"):
                try:
                    with open(os.path.join(s_dir, file), "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    py_file = os.path.join(s_dir, f"{meta.get('name')}.py")
                    code = ""
                    if os.path.exists(py_file):
                        with open(py_file, "r", encoding="utf-8") as pf:
                            code = pf.read()
                    meta["code"] = code
                    result.append(SkillItem(**meta))
                except Exception:
                    continue
        return result

    def get_skill(self, bot_id: str, name: str) -> SkillItem | None:
        s_dir = self._skills_dir(bot_id)
        clean_name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
        meta_file = os.path.join(s_dir, f"{clean_name}.json")
        py_file = os.path.join(s_dir, f"{clean_name}.py")
        if not os.path.exists(meta_file):
            return None
        with open(meta_file, "r", encoding="utf-8") as f:  # pi-lens-ignore: unchecked-throwing-call-python -- persistence boundary
            meta = json.load(f)  # pi-lens-ignore: unchecked-throwing-call-python -- persistence boundary
        code = ""
        if os.path.exists(py_file):
            with open(py_file, "r", encoding="utf-8") as pf:  # pi-lens-ignore: unchecked-throwing-call-python -- persistence boundary
                code = pf.read()
        meta["code"] = code
        return SkillItem(**meta)

    def save_skill(self, bot_id: str, name: str, description: str, code: str, input_schema: dict | None = None) -> SkillItem:
        with self._lock:
            s_dir = self._skills_dir(bot_id)
            clean_name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
            meta_file = os.path.join(s_dir, f"{clean_name}.json")
            py_file = os.path.join(s_dir, f"{clean_name}.py")

            skill = SkillItem(
                name=clean_name,
                description=description,
                code=code,
                input_schema=input_schema or {},
                created_at=now_iso()
            )
            with open(py_file, "w", encoding="utf-8") as f:  # pi-lens-ignore: unchecked-throwing-call-python -- persistence boundary
                f.write(code)
            _atomic_dump_json(meta_file, {
                "name": skill.name,
                "description": skill.description,
                "input_schema": skill.input_schema,
                "created_at": skill.created_at
            })
            return skill

    def delete_skill(self, bot_id: str, name: str) -> bool:
        with self._lock:
            s_dir = self._skills_dir(bot_id)
            clean_name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
            meta_file = os.path.join(s_dir, f"{clean_name}.json")
            py_file = os.path.join(s_dir, f"{clean_name}.py")
            removed = False
            if os.path.exists(meta_file):
                os.remove(meta_file)  # pi-lens-ignore: unchecked-throwing-call-python -- persistence boundary
                removed = True
            if os.path.exists(py_file):
                os.remove(py_file)  # pi-lens-ignore: unchecked-throwing-call-python -- persistence boundary
                removed = True
            return removed

    # -------------------------------------------------------------
    # 会话历史与文件关联持久化
    # -------------------------------------------------------------
    def get_history(self, bot_id: str, limit: int = 50, before_id: str | None = None, user_id: str | None = None) -> list[dict[str, Any]]:
        """获取指定数量的历史消息（支持 before_id 向前翻页）"""
        return self.get_history_paged(bot_id, limit=limit, before_id=before_id, user_id=user_id)["history"]

    def get_history_paged(self, bot_id: str, limit: int = 50, before_id: str | None = None, user_id: str | None = None) -> dict[str, Any]:
        """分页获取会话历史，返回 history, total 以及 has_more 状态供前端无限滚动"""
        with self._lock:
            self.ensure_legacy_state(bot_id, user_id)
            jsonl_path = self._migrate_history_to_jsonl_unlocked(bot_id, user_id)
        with self._history_session_lock(bot_id, user_id):
            if os.path.exists(jsonl_path):
                return self._read_history_jsonl_unlocked(jsonl_path, limit, before_id)
        with self._lock:
            hist_path = self._history_path(bot_id, user_id)
            if not os.path.exists(hist_path):
                return {"history": [], "total": 0, "has_more": False}
            try:
                if os.path.getsize(hist_path) > MAX_HISTORY_TOTAL_BYTES:
                    # Do not json.load an unbounded legacy/corrupt file.  The
                    # operator can archive or repair it offline before retrying.
                    return {"history": [], "total": 0, "has_more": False, "truncated": True}
                with open(hist_path, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                return {"history": [], "total": 0, "has_more": False}

            total = len(history)
            if before_id:
                idx = next((i for i, m in enumerate(history) if m.get("id") == before_id), -1)
                if idx > 0:
                    start_idx = max(0, idx - limit)
                    return {
                        "history": history[start_idx:idx],
                        "total": total,
                        "has_more": start_idx > 0
                    }
                return {"history": [], "total": total, "has_more": False}
            else:
                start_idx = max(0, total - limit)
                return {
                    "history": history[start_idx:],
                    "total": total,
                    "has_more": start_idx > 0
                }

    def append_history(self, bot_id: str, role: str, content: str, metadata: dict | None = None, user_id: str | None = None):
        with self._lock:
            self.ensure_legacy_state(bot_id, user_id)
            jsonl_path = self._migrate_history_to_jsonl_unlocked(bot_id, user_id)
            msg = {
                "id": f"msg-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}",
                "role": role,
                "content": _truncate_history_text(content),
                "metadata": _bound_history_metadata(metadata),
                "timestamp": now_iso()
            }
        with self._history_session_lock(bot_id, user_id):
            parent = os.path.dirname(jsonl_path)
            os.makedirs(parent, mode=0o700, exist_ok=True)
            if not os.path.exists(jsonl_path):
                with open(jsonl_path, "ab"):
                    pass
                os.chmod(jsonl_path, 0o600)
            self._load_history_index_unlocked(jsonl_path)
            encoded = (json.dumps(msg, ensure_ascii=False, default=str, separators=(",", ":")) + "\n").encode("utf-8")
            with open(jsonl_path, "ab") as stream:
                offset = stream.tell()
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(jsonl_path, 0o600)
            index_path = self._history_index_path(bot_id, user_id)
            with open(index_path, "a", encoding="utf-8") as index:
                index.write(json.dumps({"id": msg["id"], "offset": offset, "end": offset + len(encoded)}, ensure_ascii=False))
                index.write("\n")
                index.flush()
                os.fsync(index.fileno())
            os.chmod(index_path, 0o600)
            if os.path.getsize(jsonl_path) > MAX_HISTORY_TOTAL_BYTES:
                self._compact_history_jsonl_unlocked(jsonl_path)

    def clear_history(self, bot_id: str, delete_files: bool = True, user_id: str | None = None):
        with self._lock:
            self.ensure_legacy_state(bot_id, user_id)
            hist_path = self._history_path(bot_id, user_id)
            jsonl_path = self._history_jsonl_path(bot_id, user_id)
            _atomic_dump_json(hist_path, [])
        with self._history_session_lock(bot_id, user_id):
            with suppress(OSError):
                os.remove(jsonl_path)
            with suppress(OSError):
                os.remove(self._history_index_path(bot_id, user_id))
            try:
                from .engine import agent_engine
                agent_engine.clear_sandbox(bot_id, user_id)
            except Exception:
                pass
            try:
                from .memory import memory_manager
                memory_manager.clear_session_memory(bot_id, self._runtime_dir(bot_id, user_id))
            except Exception:
                pass
            if delete_files:
                uploads_dir = self._uploads_dir(bot_id, user_id)
                if os.path.exists(uploads_dir):
                    for fn in os.listdir(uploads_dir):  # pi-lens-ignore: unchecked-throwing-call-python -- persistence boundary
                        try:
                            fp = os.path.join(uploads_dir, fn)
                            if os.path.isfile(fp):
                                os.remove(fp)
                        except Exception:
                            pass

    @staticmethod
    def _collect_referenced_filenames(history: list[dict[str, Any]]) -> set[str]:
        """Collect file names referenced by attachments and rendered download links."""
        referenced_filenames: set[str] = set()
        markdown_file_pattern = re.compile(
            r"/api/agent/(?:bots/[^/\s?#]+/)?files/([^/?#)\s]+)"
        )

        def add_markdown_file_references(value: Any) -> None:
            if not isinstance(value, str):
                return
            for match in markdown_file_pattern.finditer(value):
                try:
                    decoded_name = unquote(match.group(1))
                except Exception:
                    continue
                safe_name = os.path.basename(decoded_name)
                if safe_name and safe_name not in {".", ".."}:
                    referenced_filenames.add(safe_name)

        for msg in history:
            if not isinstance(msg, dict):
                continue
            add_markdown_file_references(msg.get("content", ""))
            meta = msg.get("metadata", {}) or {}
            if not isinstance(meta, dict):
                continue
            for att in meta.get("attachments", []) or []:
                if not isinstance(att, dict):
                    continue
                saved_name = att.get("saved_name")
                if saved_name:
                    referenced_filenames.add(os.path.basename(str(saved_name)))
                path = att.get("path", "")
                if path:
                    referenced_filenames.add(os.path.basename(str(path)))
                name = att.get("name")
                if name:
                    referenced_filenames.add(os.path.basename(str(name)))
                add_markdown_file_references(att.get("url"))
                add_markdown_file_references(att.get("download_url"))
        return referenced_filenames

    def cleanup_unreferenced_files(self, bot_id: str, user_id: str | None = None) -> int:
        """清理 uploads 目录下未在当前 history 中引用的孤儿文件。

        Markdown answers commonly contain generated-file download links while
        the corresponding attachment metadata is only present on an earlier
        execution step.  Keep those links alive, and give newly-created files
        a 24-hour grace period so cleanup cannot race an in-flight response or
        a client that has not persisted its history yet.
        """
        with self._lock:
            self.ensure_legacy_state(bot_id, user_id)
            uploads_dir = self._uploads_dir(bot_id, user_id)
            if not os.path.exists(uploads_dir):
                return 0

            referenced_filenames: set[str] = set()
            try:
                history = self.get_history(bot_id, limit=5000, user_id=user_id)
                referenced_filenames = self._collect_referenced_filenames(history)
            except Exception:
                pass

            deleted_count = 0
            recent_cutoff = time.time() - 24 * 60 * 60
            for fn in os.listdir(uploads_dir):  # pi-lens-ignore: unchecked-throwing-call-python -- persistence boundary
                is_ref = fn in referenced_filenames or any(fn.endswith(f"_{ref}") for ref in referenced_filenames)
                if not is_ref:
                    file_full_path = os.path.join(uploads_dir, fn)
                    try:
                        # New files get a grace period.  This also covers
                        # generated files whose history entry is appended
                        # only after the streaming response completes.
                        if os.path.isfile(file_full_path) and os.path.getmtime(file_full_path) >= recent_cutoff:
                            continue
                        if os.path.isfile(file_full_path):
                            os.remove(file_full_path)
                            deleted_count += 1
                    except Exception:
                        pass
            return deleted_count

    # -------------------------------------------------------------
    # 智能体审计、统计与性能分析
    # -------------------------------------------------------------
    def record_run_stats(self, bot_id: str, metrics: dict[str, Any]):
        """记录单次对话运行产生的消耗与耗时，持久化累计指标"""
        with self._lock:
            bot = self.get_bot(bot_id)
            if not bot:
                return
            if not hasattr(bot, "stats") or bot.stats is None:
                bot.stats = BotStats()
            bot.stats.total_runs += 1
            bot.stats.total_tokens += int(metrics.get("total_tokens", 0))
            bot.stats.total_code_executions += int(metrics.get("code_executions", 0))
            bot.stats.total_duration_seconds += round(float(metrics.get("elapsed_seconds", 0.0)), 2)
            self.save_bot(bot)

    # -------------------------------------------------------------
    # 消息回溯、重新生成与历史截断
    # -------------------------------------------------------------
    def get_last_user_message(self, bot_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        """查找最近一条用户输入消息（含附件）"""
        history = self.get_history(bot_id, limit=20, user_id=user_id)
        for msg in reversed(history):
            if msg.get("role") == "user":
                return msg
        return None

    def pop_last_assistant_message(self, bot_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        """移除最后一条助手消息（用于触发重新生成）"""
        with self._lock:
            self.ensure_legacy_state(bot_id, user_id)
            jsonl_path = self._migrate_history_to_jsonl_unlocked(bot_id, user_id)
        with self._history_session_lock(bot_id, user_id):
            if not os.path.exists(jsonl_path):
                return None
            try:
                entries = self._load_history_index_unlocked(jsonl_path)
                history = self._read_history_page_from_index_unlocked(jsonl_path, entries, 0, len(entries))
                if history and history[-1].get("role") == "assistant":
                    popped = history.pop()
                    self._replace_history_jsonl_unlocked(jsonl_path, history)
                    return popped
            except Exception:
                pass
            return None

    def truncate_history(self, bot_id: str, keep_up_to_message_id: str, user_id: str | None = None) -> bool:
        """截断历史记录至指定消息 ID（保留该消息及之前的内容，丢弃后续内容）"""
        with self._lock:
            self.ensure_legacy_state(bot_id, user_id)
            jsonl_path = self._migrate_history_to_jsonl_unlocked(bot_id, user_id)
        with self._history_session_lock(bot_id, user_id):
            if not os.path.exists(jsonl_path):
                return False
            try:
                entries = self._load_history_index_unlocked(jsonl_path)
                history = self._read_history_page_from_index_unlocked(jsonl_path, entries, 0, len(entries))
                idx = next((i for i, m in enumerate(history) if m.get("id") == keep_up_to_message_id), -1)
                if idx >= 0:
                    truncated = history[: idx + 1]
                    self._replace_history_jsonl_unlocked(jsonl_path, truncated)
                    return True
            except Exception:
                pass
            return False

    # -------------------------------------------------------------
    # 产出物与文件管理中心 (Artifact / File Hub)
    # -------------------------------------------------------------
    def list_bot_files(self, bot_id: str, user_id: str | None = None) -> list[dict[str, Any]]:
        """查询指定智能体所有的上传与产出文件，带有大小、时间与引用状态"""
        with self._lock:
            self.ensure_legacy_state(bot_id, user_id)
            bot_dir = self._runtime_dir(bot_id, user_id)
            uploads_dir = os.path.join(bot_dir, "uploads")
            os.makedirs(uploads_dir, exist_ok=True)  # pi-lens-ignore: unchecked-throwing-call-python -- persistence boundary

            # 获取所有在 history 中被引用的文件名，包括正文下载链接
            referenced_filenames: set[str] = set()
            try:
                history = self.get_history(bot_id, limit=5000, user_id=user_id)
                referenced_filenames = self._collect_referenced_filenames(history)
            except Exception:
                pass

            file_list = []
            for fn in os.listdir(uploads_dir):  # pi-lens-ignore: unchecked-throwing-call-python -- persistence boundary
                fp = os.path.join(uploads_dir, fn)
                if not os.path.isfile(fp):
                    continue
                try:
                    stat = os.stat(fp)
                    size = stat.st_size
                    mtime = stat.st_mtime
                    created_iso = datetime.fromtimestamp(mtime, tz=UTC).isoformat()
                    cat, ftype = get_file_category_and_type(fn)
                    orig_name = re.sub(r"^f_\d+_", "", fn)
                    is_ref = fn in referenced_filenames or any(fn.endswith(f"_{ref}") for ref in referenced_filenames)

                    file_list.append({
                        "name": fn,
                        "original_name": orig_name,
                        "size": size,
                        "size_formatted": format_file_size(size),
                        "category": cat,
                        "type": ftype,
                        "url": f"/api/agent/bots/{bot_id}/files/{fn}",
                        "created_at": created_iso,
                        "is_referenced": is_ref
                    })
                except Exception:
                    continue

            # 按时间倒序（最新排在最前）
            file_list.sort(key=lambda x: x["created_at"], reverse=True)
            return file_list

    def delete_bot_file(self, bot_id: str, filename: str, user_id: str | None = None) -> bool:
        """物理删除智能体专属目录下的单个文件"""
        with self._lock:
            self.ensure_legacy_state(bot_id, user_id)
            bot_dir = self._runtime_dir(bot_id, user_id)
            safe_name = os.path.basename(filename)
            uploads_dir = os.path.join(bot_dir, "uploads")
            fp = os.path.join(uploads_dir, safe_name)
            if os.path.exists(fp) and os.path.isfile(fp):
                try:
                    os.remove(fp)
                    return True
                except Exception:
                    return False
            # 兼容按原始文件名删除 (如 image.png 匹配到 f_xxx_image.png)
            if os.path.exists(uploads_dir):
                for fn in os.listdir(uploads_dir):  # pi-lens-ignore: unchecked-throwing-call-python -- persistence boundary
                    if fn == safe_name or fn.endswith(f"_{safe_name}"):
                        target = os.path.join(uploads_dir, fn)
                        if os.path.isfile(target):
                            try:
                                os.remove(target)
                                return True
                            except Exception:
                                return False
            return False

    # -------------------------------------------------------------
    # 多智能体协同链路与拓扑记录 (Multi-Agent Orchestration & Topology)
    # -------------------------------------------------------------
    def _orchestrations_path(self) -> str:
        return os.path.join(self.data_dir, "orchestrations.json")

    def record_orchestration(self, record: dict[str, Any]):
        """记录一条多智能体协同调用事件"""
        with self._lock:
            path = self._orchestrations_path()
            events = []
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        events = json.load(f)
                except Exception:
                    events = []
            events.insert(0, record)
            # 保留最近 500 次协同记录
            events = events[:500]
            _atomic_dump_json(path, events)

    def get_orchestrations(self, limit: int = 50, user_id: str | None = None, is_admin: bool = False) -> list[dict[str, Any]]:
        """获取最近的多智能体协同流水；非管理员仅返回自己的事件。"""
        with self._lock:
            path = self._orchestrations_path()
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        events = json.load(f)
                    if not is_admin and user_id:
                        events = [event for event in events if event.get("owner_id") == user_id]
                    return events[:limit]
                except Exception:
                    return []
            return []

    def get_orchestration_topology(self) -> dict[str, Any]:
        """
        计算全局多智能体协同网络拓扑结构：
        - nodes: 所有注册的智能体节点
        - links: 智能体之间的协同调用拓扑边（频次、最后调用时间、最近任务摘要）
        - recent_events: 最近协同流水
        """
        with self._lock:
            bots = self.list_bots()
            nodes = [
                {
                    "id": b["id"],
                    "name": b.get("name", ""),
                    "avatar": b.get("avatar", "🤖"),
                    "description": b.get("description", ""),
                    "skills_count": b.get("skills_count", 0),
                    "runs_count": b.get("stats", {}).get("total_runs", 0) if b.get("stats") else 0,
                    "is_default": b["id"] == "default_assistant"
                }
                for b in bots
            ]
            bot_map = {n["id"]: n for n in nodes}

            # 聚合调用关系
            events = self.get_orchestrations(limit=200)
            edge_map: dict[str, dict[str, Any]] = {}

            for ev in events:
                c_id = ev.get("caller_bot_id")
                t_id = ev.get("target_bot_id")
                if not c_id or not t_id or c_id == t_id:
                    continue
                edge_key = f"{c_id}->{t_id}"
                if edge_key not in edge_map:
                    edge_map[edge_key] = {
                        "source": c_id,
                        "target": t_id,
                        "source_name": ev.get("caller_bot_name") or bot_map.get(c_id, {}).get("name", c_id),
                        "target_name": ev.get("target_bot_name") or bot_map.get(t_id, {}).get("name", t_id),
                        "calls_count": 0,
                        "last_task": ev.get("task", ""),
                        "last_status": ev.get("status", "success"),
                        "last_timestamp": ev.get("timestamp", ""),
                        "elapsed_seconds": ev.get("elapsed_seconds", 0)
                    }
                edge_map[edge_key]["calls_count"] += 1
                if not edge_map[edge_key]["last_task"]:
                    edge_map[edge_key]["last_task"] = ev.get("task", "")

            # 确保即使尚无历史调用，若有 default_assistant 和其他专业 bot，也展示拓扑待命边
            default_bot = next((b for b in bots if b["id"] == "default_assistant"), None)
            if default_bot:
                for b in bots:
                    if b["id"] != "default_assistant":
                        key = f"default_assistant->{b['id']}"
                        if key not in edge_map:
                            edge_map[key] = {
                                "source": "default_assistant",
                                "target": b["id"],
                                "source_name": default_bot.get("name", "TIM 全能助手"),
                                "target_name": b.get("name", ""),
                                "calls_count": 0,
                                "last_task": "协同就绪待命",
                                "last_status": "ready",
                                "last_timestamp": "",
                                "elapsed_seconds": 0
                            }

            return {
                "nodes": nodes,
                "links": list(edge_map.values()),
                "recent_events": events[:20]
            }



# 单例实例
bot_manager = BotManager()
