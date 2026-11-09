# ruff: noqa
"""
智能体 REST & SSE 路由：多 BOT 管理、流式对话、记忆库、自编技能与自增强准则
"""

import asyncio
import base64
import binascii
import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
import re
import socket
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import suppress
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse, urlunparse

import requests
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from core.agent.bot_manager import BotIdConflict, bot_manager
from core.agent.tools import MAX_SKILL_CODE_BYTES, _validate_skill_ast
from core.auth import CurrentUser, current_user, require_admin, require_operator
from core.agent.constants import SUPPORTED_PROVIDERS, BOT_PRESET_TEMPLATES, HIGH_RISK_API_KEYWORDS
from core.agent.risk import matching_confirmation_paths, resolve_api
from core.agent.engine import agent_engine
from core.agent.file_parser import parse_file_summary, format_file_size
from core.agent.models import BotConfig, LLMConfig
from core.agent import model_url_guard
from core.agent.rag_manager import rag_manager
from core.agent.run_manager import AgentRun, MAX_EVENTS_PER_RUN, agent_run_manager
from core.tim_client import TimClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["AI 智能体"])


def require_bot_access(bot_id: str, user: CurrentUser = Depends(current_user)) -> CurrentUser:
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="BOT 不存在")
    if not bot_manager.bot_scope_matches(
        bot,
        issuer=user.issuer,
        tenant_id=user.tenant_id,
    ):
        raise HTTPException(status_code=404, detail="BOT 不存在")
    if user.is_admin:
        return user
    if bot.visibility == "public":
        return user
    if bot.owner_id and bot.owner_id == user.subject:
        return user
    raise HTTPException(status_code=403, detail="无权访问该 BOT")

def require_bot_definition_editor(bot_id: str, user: CurrentUser = Depends(current_user)) -> CurrentUser:
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="BOT 不存在")
    if not bot_manager.bot_scope_matches(
        bot,
        issuer=user.issuer,
        tenant_id=user.tenant_id,
    ):
        raise HTTPException(status_code=404, detail="BOT 不存在")
    if user.is_admin:
        return user
    if bot.visibility == "public":
        raise HTTPException(status_code=403, detail="公共 BOT 定义仅管理员可修改")
    if bot.owner_id == user.subject:
        return user
    raise HTTPException(status_code=403, detail="无权修改该 BOT")


def require_scoped_admin(bot_id: str, user: CurrentUser = Depends(current_user)) -> CurrentUser:
    """Require an administrator who belongs to the BOT's issuer/tenant scope."""
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="BOT 不存在")
    if not bot_manager.bot_scope_matches(
        bot,
        issuer=user.issuer,
        tenant_id=user.tenant_id,
    ):
        raise HTTPException(status_code=404, detail="BOT 不存在")
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


MAX_AGENT_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_KNOWLEDGE_UPLOAD_BYTES = 20 * 1024 * 1024
# Bound both the number and aggregate size of files an export can materialize
# in memory.  The per-file upload limit alone does not bound generated files
# or a long-lived bot's total export size.
MAX_AGENT_FILES_PER_BOT = 1000
MAX_AGENT_EXPORT_BYTES = 100 * 1024 * 1024
MAX_ATTACHMENT_META_BYTES = 128 * 1024
# The upload endpoint embeds image files up to 3 MiB as a base64 data URL.
# Base64 expands that payload to roughly 4 MiB; leave room for the data URL
# prefix and a small amount of content-type variation at the request boundary.
MAX_ATTACHMENT_DATA_URL_LENGTH = 5 * 1024 * 1024
# SVG is an active XML format.  It is safe to preview only after parsing and
# removing script-capable elements/attributes and external references.  Keep
# this separate from the general upload limit so sanitisation cannot allocate
# an unbounded in-memory document.
MAX_SAFE_SVG_BYTES = 5 * 1024 * 1024
MAX_PROVIDER_NAME_CHARS = 64
MAX_MODEL_NAME_CHARS = 256
MAX_MODEL_URL_CHARS = 2048
MAX_MODEL_API_KEY_CHARS = 4096
MAX_MODEL_OVERRIDE_BYTES = 32 * 1024
MAX_BOT_NAME_CHARS = 128
MAX_BOT_GOAL_CHARS = 4_096
MAX_BOT_DESCRIPTION_CHARS = 8_192
MAX_BOT_PROMPT_CHARS = 65_536
MAX_BOT_PROMPT_ITEM_CHARS = 1_024
MAX_MEMORY_KEY_CHARS = 256
MAX_MEMORY_CONTENT_CHARS = 16_384
MAX_GUIDELINE_INSTRUCTION_CHARS = 16_384
MAX_GUIDELINE_REASON_CHARS = 4_096
MAX_KNOWLEDGE_QUERY_CHARS = 4_096


def _bound_json_bytes(value: Optional[Dict[str, Any]], *, label: str) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    if len(encoded) > MAX_MODEL_OVERRIDE_BYTES:
        raise ValueError(f"{label}超过 {MAX_MODEL_OVERRIDE_BYTES // 1024} KiB 限制")
    return value


def _write_agent_audit(
    user: CurrentUser | None,
    *,
    action: str,
    bot_id: str,
    outcome: str = "SUCCESS",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record bounded file/agent events through the active BOT database."""
    try:
        store = getattr(bot_manager, "audit_store", None)
        if store is None:
            return
        store.append(
            action=action,
            resource_type="agent_bot",
            resource_id=bot_id,
            outcome=outcome,
            actor_subject=user.subject if isinstance(user, CurrentUser) else "",
            issuer=user.issuer if isinstance(user, CurrentUser) else "",
            tenant_id=user.tenant_id if isinstance(user, CurrentUser) else "",
            metadata=metadata or {},
        )
    except Exception as exc:  # noqa: BLE001 - audit failure must be observable
        logger.warning("agent audit event write failed: %s", exc)


_SVG_DANGEROUS_TAGS = {
    "script",
    "base",
    "foreignobject",
    "iframe",
    "object",
    "embed",
    "link",
    "audio",
    "video",
    "style",
    "animate",
    "animatemotion",
    "animatetransform",
    "discard",
    "mpath",
    "set",
}
_SVG_URI_ATTRIBUTES = {"href", "src", "action", "formaction", "poster"}

# Inline SVG styles are useful for exported drawings (for example
# ``fill:#0f172a;stroke-width:2``), but SVG-capable browsers also interpret
# CSS URLs and a handful of legacy CSS properties as active content.  Keep a
# presentation-property allowlist so a future CSS property cannot silently
# become an execution sink.  ``url(#id)`` is handled separately below and is
# limited to same-document fragments.
_SVG_SAFE_STYLE_PROPERTIES = {
    "alignment-baseline",
    "baseline-shift",
    "clip",
    "clip-path",
    "clip-rule",
    "color",
    "color-interpolation",
    "color-interpolation-filters",
    "color-rendering",
    "cursor",
    "direction",
    "display",
    "dominant-baseline",
    "enable-background",
    "fill",
    "fill-opacity",
    "fill-rule",
    "filter",
    "flood-color",
    "flood-opacity",
    "font-family",
    "font-size",
    "font-size-adjust",
    "font-stretch",
    "font-style",
    "font-variant",
    "font-weight",
    "glyph-orientation-horizontal",
    "glyph-orientation-vertical",
    "image-rendering",
    "isolation",
    "letter-spacing",
    "lighting-color",
    "marker-end",
    "marker-mid",
    "marker-start",
    "mask",
    "mask-type",
    "mix-blend-mode",
    "opacity",
    "overflow",
    "paint-order",
    "pointer-events",
    "shape-rendering",
    "stop-color",
    "stop-opacity",
    "stroke",
    "stroke-dasharray",
    "stroke-dashoffset",
    "stroke-linecap",
    "stroke-linejoin",
    "stroke-miterlimit",
    "stroke-opacity",
    "stroke-width",
    "text-anchor",
    "text-decoration",
    "text-rendering",
    "transform",
    "transform-box",
    "transform-origin",
    "unicode-bidi",
    "vector-effect",
    "visibility",
    "white-space",
    "word-spacing",
    "writing-mode",
}


def _split_svg_style_declarations(value: str) -> Optional[List[str]]:
    """Split a CSS declaration list without treating quoted ``;`` as syntax.

    This is deliberately a small parser rather than a regular expression:
    CSS allows parentheses and quoted strings in values.  Backslash escapes,
    comments, and control characters are rejected by the caller because they
    are common ways to hide a dangerous token from a blacklist.
    """
    declarations: List[str] = []
    start = 0
    quote: Optional[str] = None
    depth = 0
    for index, char in enumerate(value):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return None
            depth -= 1
        elif char == ";" and depth == 0:
            declarations.append(value[start:index])
            start = index + 1
    if quote or depth:
        return None
    declarations.append(value[start:])
    return declarations


def _is_safe_svg_local_url_target(value: str) -> bool:
    target = value.strip()
    if len(target) >= 2 and target[0] in ("'", '"') and target[-1] == target[0]:
        target = target[1:-1].strip()
    return bool(re.fullmatch(r"#[A-Za-z0-9_.:-]{1,256}", target))


def _is_safe_svg_raster_data_url(value: str, *, max_bytes: int) -> bool:
    """Accept only well-formed, magic-byte-checked raster data URLs."""
    prefix, separator, encoded = value.partition(",")
    if not separator or len(value) > max_bytes or ";base64" not in prefix.lower():
        return False
    media_type = prefix.split(":", 1)[-1].split(";", 1)[0].lower()
    magic_by_type = {
        "image/png": (b"\x89PNG\r\n\x1a\n",),
        "image/jpeg": (b"\xff\xd8\xff",),
        "image/gif": (b"GIF87a", b"GIF89a"),
        "image/webp": (b"RIFF",),
    }
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return False
    if media_type not in magic_by_type or not any(decoded.startswith(magic) for magic in magic_by_type[media_type]):
        return False
    if media_type == "image/webp" and len(decoded) < 12:
        return False
    return media_type != "image/webp" or decoded[8:12] == b"WEBP"


def _sanitize_svg_style(value: str) -> Optional[str]:
    """Keep safe inline SVG CSS declarations and discard active ones.

    The returned string contains only allowlisted presentation properties.  A
    declaration containing a URL is retained only when the URL is a local
    fragment (``url(#gradient)``); all external/data/script URLs, CSS imports,
    expressions, custom-property indirection, escapes, and malformed syntax
    are discarded.  Returning ``None`` means no safe declarations remain.
    """
    if not isinstance(value, str) or not value or len(value) > 8192:
        return None
    # Reject CSS comments, escapes, control characters, and characters that
    # would turn a declaration into a nested rule or markup.  Whitespace is
    # safe and is retained for readable output.
    if (
        "/*" in value
        or "*/" in value
        or "\\" in value
        or any(ord(char) < 0x20 and char not in "\t\n\r\f" for char in value)
        or any(char in value for char in "{}<>")
    ):
        return None

    declarations = _split_svg_style_declarations(value)
    if declarations is None:
        return None

    safe: List[str] = []
    for declaration in declarations:
        declaration = declaration.strip()
        if not declaration:
            continue
        # Find the property/value separator outside a quoted string or
        # function.  A colon inside ``url(...)`` or a quoted font name is not
        # a declaration separator.
        quote: Optional[str] = None
        depth = 0
        separator = -1
        for index, char in enumerate(declaration):
            if quote:
                if char == quote:
                    quote = None
                continue
            if char in ("'", '"'):
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == ":" and depth == 0:
                separator = index
                break
        if separator <= 0 or quote or depth:
            continue

        property_name = declaration[:separator].strip().lower()
        property_value = declaration[separator + 1 :].strip()
        if (
            property_name not in _SVG_SAFE_STYLE_PROPERTIES
            or not property_value
            or not re.fullmatch(r"-?[a-z][a-z0-9-]*", property_name)
        ):
            continue

        # Normalize only for inspection.  CSS whitespace can otherwise split
        # javascript:/expression() tokens across lines.  Backslashes and
        # comments were rejected above, so this catches their plain forms and
        # cannot be bypassed with a CSS escape.
        normalized_value = re.sub(r"\s+", "", property_value).lower()
        if any(
            token in normalized_value
            for token in (
                "javascript:",
                "vbscript:",
                "data:",
                "expression(",
                "var(",
                "-moz-binding",
                "behavior:",
                "progid:",
                "@import",
            )
        ):
            continue
        if "@" in property_value:
            continue

        # CSS URL functions are only useful for same-document SVG resources.
        # Do not accept a bare ``url`` token or any external/data URL.  The
        # exact shape also prevents nested functions and quote tricks.
        urls = re.findall(r"url\s*\(([^)]*)\)", property_value, flags=re.IGNORECASE)
        if re.search(r"url\s*\(", property_value, flags=re.IGNORECASE):
            if not urls or any(not _is_safe_svg_local_url_target(target) for target in urls):
                continue
            if re.sub(r"url\s*\([^)]*\)", "", property_value, flags=re.IGNORECASE).lower().find("url") >= 0:
                continue
        elif re.search(r"\burl\b", normalized_value, flags=re.IGNORECASE):
            continue

        safe.append(f"{property_name}:{property_value}")

    return ";".join(safe) or None


def _sanitize_svg_stylesheet(value: str) -> Optional[str]:
    """Filter a small, static subset of an SVG ``<style>`` stylesheet.

    SVG exports commonly put presentation declarations in rules such as
    ``.background{fill:#fff}``.  At-rules (including ``@import`` and
    ``@font-face``) and nested CSS are intentionally unsupported; retaining
    only selector/declaration blocks keeps the stylesheet useful without
    exposing browser-specific CSS execution features.
    """
    if not isinstance(value, str) or not value or len(value) > 64 * 1024:
        return None
    if (
        "/*" in value
        or "*/" in value
        or "\\" in value
        or any(ord(char) < 0x20 and char not in "\t\n\r\f" for char in value)
        or any(char in value for char in "<>")
    ):
        return None

    rules: List[str] = []
    cursor = 0
    length = len(value)
    while cursor < length:
        while cursor < length and value[cursor].isspace():
            cursor += 1
        if cursor >= length:
            break

        # Find the next top-level rule opener/terminator.  A top-level
        # semicolon belongs to an at-rule such as @import; discard that
        # at-rule and continue so a later safe rule is still useful.
        quote: Optional[str] = None
        paren_depth = 0
        opener = -1
        terminator = ""
        index = cursor
        while index < length:
            char = value[index]
            if quote:
                if char == quote:
                    quote = None
            elif char in ("'", '"'):
                quote = char
            elif char == "(":
                paren_depth += 1
            elif char == ")":
                if paren_depth == 0:
                    return None
                paren_depth -= 1
            elif paren_depth == 0 and char in "{};":
                opener = index
                terminator = char
                break
            index += 1
        if quote or paren_depth or opener == -1:
            return None
        if terminator == ";":
            # Only at-rules may appear between ordinary rules.  A stray
            # declaration or selector is malformed and causes the style
            # element to be dropped.
            if not value[cursor:opener].lstrip().startswith("@"):
                return None
            cursor = opener + 1
            continue
        if terminator == "}":
            return None

        selector = value[cursor:opener].strip()
        body_start = opener + 1
        index = body_start
        quote = None
        paren_depth = 0
        nested_depth = 0
        closer = -1
        while index < length:
            char = value[index]
            if quote:
                if char == quote:
                    quote = None
            elif char in ("'", '"'):
                quote = char
            elif char == "(":
                paren_depth += 1
            elif char == ")":
                if paren_depth == 0:
                    return None
                paren_depth -= 1
            elif paren_depth == 0 and char == "{":
                nested_depth += 1
            elif paren_depth == 0 and char == "}":
                if nested_depth:
                    nested_depth -= 1
                else:
                    closer = index
                    break
            index += 1
        if quote or paren_depth or closer == -1:
            return None

        # At-rules with blocks (for example @font-face/@media) and nested
        # blocks are skipped as a unit.  The preceding and following ordinary
        # rules remain available for preview.
        if (
            not selector
            or ";" in selector
            or selector.lstrip().startswith("@")
            or nested_depth
            or re.search(r"(?:url|javascript|vbscript|data)\s*\(", selector, flags=re.IGNORECASE)
        ):
            cursor = closer + 1
            continue

        declarations = _sanitize_svg_style(value[body_start:closer])
        if declarations:
            rules.append(f"{selector}{{{declarations}}}")
        cursor = closer + 1

    return "".join(rules) or None


def _svg_local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].lower()


def sanitize_svg(content: bytes, *, max_bytes: int = MAX_SAFE_SVG_BYTES) -> bytes:
    """Return a conservative, non-active SVG suitable for an ``<img>``.

    SVG files are user-controlled XML and may contain script, event handlers,
    CSS, or external references.  We reject DTD/entity constructs before
    parsing (the stdlib parser is not an HTML sanitizer), remove active nodes
    and event attributes, retain only filtered static CSS declarations, and
    keep URI attributes only for local fragments or embedded raster images.
    Callers should fall back to an attachment when this validation fails.
    """
    if not isinstance(content, (bytes, bytearray)) or len(content) > max_bytes:
        raise ValueError("SVG 文件超过安全预览大小限制")
    raw = bytes(content)
    lowered = raw.lower()
    # Do not let an XML entity/stylesheet declaration reach ElementTree.  This
    # also rejects external entities and the common billion-laughs payload.
    if any(marker in lowered for marker in (b"<!doctype", b"<!entity", b"<?xml-stylesheet")):
        raise ValueError("SVG 包含不允许的 XML 声明")
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError, UnicodeError) as exc:
        raise ValueError("SVG XML 结构无效") from exc
    if _svg_local_name(root.tag) != "svg":
        raise ValueError("SVG 根元素无效")

    def scrub(element: ET.Element) -> None:
        for child in list(element):
            child_local = _svg_local_name(child.tag)
            if child_local == "style":
                # Keep only ordinary selector/declaration rules.  A style
                # element with nested XML, unsupported at-rules, or no safe
                # declarations is removed as active content.
                if list(child) or not child.text:
                    element.remove(child)
                    continue
                safe_stylesheet = _sanitize_svg_stylesheet(child.text)
                if safe_stylesheet is None:
                    element.remove(child)
                    continue
                child.attrib.clear()
                child.text = safe_stylesheet
            elif child_local in _SVG_DANGEROUS_TAGS:
                element.remove(child)
            else:
                scrub(child)

        for attr_name, attr_value in list(element.attrib.items()):
            local = _svg_local_name(attr_name)
            value = str(attr_value).strip()
            lowered_value = value.lower()
            # Event attributes (onload, onclick, ...) are executable in
            # SVG-capable user agents.  Inline CSS is filtered declaration by
            # declaration so static presentation styles remain usable.
            if local.startswith("on"):
                element.attrib.pop(attr_name, None)
                continue
            if local == "style":
                safe_style = _sanitize_svg_style(value)
                if safe_style is None:
                    element.attrib.pop(attr_name, None)
                else:
                    element.attrib[attr_name] = safe_style
                continue
            # URI-bearing values can occur in presentation attributes too
            # (fill/filter/clip-path).  Keep only local fragment references;
            # external URLs and javascript/data markup are removed globally.
            if (
                any(scheme in lowered_value for scheme in ("javascript:", "vbscript:", "data:text/", "data:image/svg"))
                or ("url(" in lowered_value and not re.fullmatch(r"url\(\s*#[^)]{1,256}\s*\)", lowered_value))
            ):
                element.attrib.pop(attr_name, None)
                continue
            if local in _SVG_URI_ATTRIBUTES:
                # Permit only same-document fragments and embedded raster
                # images.  SVG-in-SVG, javascript:, http(s), and data URLs
                # containing markup are intentionally not previewable.
                safe_local = value.startswith("#") and len(value) <= 256
                safe_raster = _is_safe_svg_raster_data_url(value, max_bytes=max_bytes)
                if not (safe_local or safe_raster):
                    element.attrib.pop(attr_name, None)

    try:
        scrub(root)
    except RecursionError as exc:
        raise ValueError("SVG 嵌套层级超过安全上限") from exc
    try:
        sanitized = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    except RecursionError as exc:
        raise ValueError("SVG 嵌套层级超过安全上限") from exc
    if len(sanitized) > max_bytes:
        raise ValueError("SVG 清洗结果超过安全预览大小限制")
    return sanitized


# -------------------------------------------------------------
# DTO 请求体定义
# -------------------------------------------------------------
class LLMConfigPayload(BaseModel):
    provider: str = Field(..., max_length=MAX_PROVIDER_NAME_CHARS, description="模型提供商：deepseek, qwen, ollama, openai, custom")
    model_name: str = Field(..., max_length=MAX_MODEL_NAME_CHARS, description="模型名称")
    base_url: str = Field(default="", max_length=MAX_MODEL_URL_CHARS, description="Base URL")
    api_key: Optional[str] = Field(default="", max_length=MAX_MODEL_API_KEY_CHARS, description="API Key")
    temperature: float = Field(default=0.2, ge=0, le=2, description="温度")
    max_steps: Optional[int] = Field(default=30, ge=1, le=200, description="最大执行步数（推荐 20~50 轮）")
    context_budget: Optional[int] = Field(default=32000, ge=1_024, le=2_000_000, description="即时工作记忆总字符预算")
    max_context_turns: Optional[int] = Field(default=25, ge=1, le=200, description="即时工作记忆保留最大轮数")


class FetchModelsPayload(BaseModel):
    provider: Optional[str] = Field(default=None, max_length=MAX_PROVIDER_NAME_CHARS, description="模型提供商")
    base_url: Optional[str] = Field(default=None, max_length=MAX_MODEL_URL_CHARS, description="Base URL")
    api_key: Optional[str] = Field(default=None, max_length=MAX_MODEL_API_KEY_CHARS, description="API Key")


class CreateBotPayload(BaseModel):
    name: str = Field(..., max_length=MAX_BOT_NAME_CHARS, description="智能体名称")
    goal: Optional[str] = Field(default="", max_length=MAX_BOT_GOAL_CHARS, description="职责与业务目标（希望智能体做什么）")
    id: Optional[str] = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_-]{1,64}$",
        description="唯一标识 (可选，留空由 AI 自动生成)",
    )
    avatar: Optional[str] = Field(default=None, max_length=32, description="头像 Emoji (可选，留空由 AI 自动匹配)")
    description: Optional[str] = Field(default=None, max_length=MAX_BOT_DESCRIPTION_CHARS, description="功能描述 (可选，留空由 AI 自动生成)")
    base_prompt: Optional[str] = Field(default=None, max_length=MAX_BOT_PROMPT_CHARS, description="基础人设提示词 (可选，留空由 AI 自动生成)")
    suggested_prompts: Optional[List[str]] = Field(default=None, max_length=8, description="个性化推荐快捷指令")
    model_override: Optional[Dict[str, Any]] = Field(default=None, description="专属模型配置")

    @field_validator("suggested_prompts")
    @classmethod
    def bound_suggested_prompts(
        cls,
        value: Optional[List[str]],
    ) -> Optional[List[str]]:
        if value is None:
            return None
        if any(len(str(item)) > MAX_BOT_PROMPT_ITEM_CHARS for item in value):
            raise ValueError(
                f"每条推荐指令不能超过 {MAX_BOT_PROMPT_ITEM_CHARS} 个字符"
            )
        return value

    @field_validator("model_override")
    @classmethod
    def bound_model_override(
        cls,
        value: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        return _bound_json_bytes(value, label="专属模型配置")


class TransferBotOwnershipPayload(BaseModel):
    owner_id: str = Field(..., min_length=1, description="目标 OIDC subject")


class LegacyStateMigrationPayload(BaseModel):
    target_user_id: str = Field(..., min_length=1, max_length=256, description="明确的目标 OIDC subject")


class GeneratePreviewPayload(BaseModel):
    name: str = Field(..., description="智能体名称")
    goal: Optional[str] = Field(default="", description="职责与业务目标")


class UpdateBotPayload(BaseModel):
    name: Optional[str] = Field(default=None, max_length=MAX_BOT_NAME_CHARS)
    avatar: Optional[str] = Field(default=None, max_length=32)
    description: Optional[str] = Field(default=None, max_length=MAX_BOT_DESCRIPTION_CHARS)
    base_prompt: Optional[str] = Field(default=None, max_length=MAX_BOT_PROMPT_CHARS)
    model_override: Optional[Dict[str, Any]] = None

    @field_validator("model_override")
    @classmethod
    def bound_model_override(
        cls,
        value: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        return _bound_json_bytes(value, label="专属模型配置")


class AttachmentItem(BaseModel):
    id: str = Field(..., max_length=256, description="附件唯一标识")
    name: str = Field(..., max_length=512, description="原始文件名")
    size: int = Field(default=0, description="文件大小（字节）")
    type: str = Field(default="file", max_length=64, description="文件格式类型，如 docx, xlsx, csv, txt, pdf, png 等")
    category: Optional[str] = Field(default="file", max_length=64, description="分类：document, table, text, image, code 等")
    path: str = Field(..., max_length=4096, description="服务器本地文件绝对路径")
    url: Optional[str] = Field(default=None, max_length=4096, description="在线访问/下载地址")
    # The server resolves attachments from its uploads directory; accepting a
    # multi-megabyte client supplied data URL only duplicates request/SSE state.
    data_url: Optional[str] = Field(
        default=None,
        max_length=MAX_ATTACHMENT_DATA_URL_LENGTH,
        description="用于图片快速缩略图的 Base64 Data URL（最多 5 MiB）",
    )
    size_formatted: Optional[str] = Field(default=None, max_length=128, description="人性化大小表示")
    summary: Optional[str] = Field(default=None, max_length=65_536, description="结构化摘要")
    preview: Optional[str] = Field(default=None, max_length=65_536, description="预览文本")
    meta: Optional[Dict[str, Any]] = Field(default=None, description="结构元数据")

    @field_validator("meta")
    @classmethod
    def bound_meta_size(cls, value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if value is None:
            return None
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        if len(encoded) > MAX_ATTACHMENT_META_BYTES:
            raise ValueError("附件元数据超过 128 KiB 限制")
        return value


class ChatPayload(BaseModel):
    message: str = Field(default="", max_length=1_048_576, description="用户输入的指令或问题")
    client_msg_id: Optional[str] = Field(default=None, description="前端指定的消息ID")
    attachments: Optional[List[AttachmentItem]] = Field(default=None, max_length=20, description="随消息发送的附件列表")


class TruncateHistoryPayload(BaseModel):
    message_id: str = Field(..., description="截断至此消息 ID（保留该消息及之前内容，丢弃后续内容）")


class MemoryPayload(BaseModel):
    key: str = Field(..., max_length=MAX_MEMORY_KEY_CHARS, description="记忆标识")
    content: str = Field(..., max_length=MAX_MEMORY_CONTENT_CHARS, description="记忆事实详情")


class GuidelinePayload(BaseModel):
    instruction: str = Field(..., max_length=MAX_GUIDELINE_INSTRUCTION_CHARS, description="指令规则内容")
    reason: Optional[str] = Field(default="", max_length=MAX_GUIDELINE_REASON_CHARS, description="强化原因")


class SaveSkillPayload(BaseModel):
    name: str = Field(..., description="技能名称")
    description: str = Field(..., description="功能描述")
    python_code: str = Field(..., max_length=MAX_SKILL_CODE_BYTES, description="Python 代码")
    input_schema: Optional[Dict[str, Any]] = Field(default_factory=dict, description="入参模式说明")

    @field_validator("python_code")
    @classmethod
    def bound_python_code_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_SKILL_CODE_BYTES:
            raise ValueError("技能代码超过 64 KiB 限制")
        return value


class RunSkillPayload(BaseModel):
    kwargs: Optional[Dict[str, Any]] = Field(default_factory=dict, description="执行入参")
    confirm_high_risk: bool = Field(default=False, description="明确确认执行高危技能")
    approved_high_risk_paths: List[str] = Field(
        default_factory=list,
        description="本次允许执行的高危 API 路径白名单；每次调用按路径逐项匹配",
    )


class ToggleKnowledgePayload(BaseModel):
    enabled: Optional[bool] = None


class SearchKnowledgePayload(BaseModel):
    query: str = Field(..., max_length=MAX_KNOWLEDGE_QUERY_CHARS, description="检索问题或关键词")
    top_k: Optional[int] = Field(default=4, ge=1, le=50, description="返回最多匹配条数")
    min_score: Optional[float] = Field(default=0.05, ge=0, le=1_000_000, description="最低相关度分数阈值")


class ApprovalDecisionPayload(BaseModel):
    decision: str = Field(..., description="决策：'approve' 或 'reject'")



# -------------------------------------------------------------
# LLM 全局模型配置与预设
# -------------------------------------------------------------
@router.get("/config")
def get_llm_config(_user=Depends(require_admin)):
    """获取大模型配置（API Key 脱敏）与支持的供应商列表 (SSoT)"""
    return {
        "config": bot_manager.get_masked_llm_config(),
        "providers": SUPPORTED_PROVIDERS
    }


@router.get("/templates")
def list_bot_templates():
    """获取预设智能体角色模板（社群风控官、消息巡检员、运维保障官等）"""
    return {
        "success": True,
        "templates": BOT_PRESET_TEMPLATES
    }


@router.get("/security/high-risk-keywords")
def get_high_risk_keywords():
    """获取高危破坏性接口安全拦截规则与关键词"""
    return {
        "success": True,
        "keywords": bot_manager.get_high_risk_keywords(),
        "default_keywords": HIGH_RISK_API_KEYWORDS
    }


class HighRiskKeywordsPayload(BaseModel):
    keywords: List[str] = Field(..., description="高危接口拦截关键词列表")


@router.post("/security/high-risk-keywords")
def update_high_risk_keywords(
    payload: HighRiskKeywordsPayload,
    user: CurrentUser = Depends(require_admin),
):
    """更新高危破坏性接口拦截关键词"""
    updated = bot_manager.set_high_risk_keywords(
        payload.keywords,
        actor_subject=user.subject,
        issuer=user.issuer,
        tenant_id=user.tenant_id,
    )
    return {
        "success": True,
        "keywords": updated
    }


@router.post("/config")
def save_llm_config(payload: LLMConfigPayload, _user=Depends(require_admin)):
    """保存大模型连接配置"""
    existing = bot_manager.get_llm_config()
    # 若前端传空 key 且原本已有 key，保留原 key 避免被误清空
    api_key = payload.api_key.strip() if payload.api_key is not None else ""
    if not api_key and existing.api_key and payload.provider == existing.provider:
        api_key = existing.api_key

    new_cfg = LLMConfig(
        provider=payload.provider,
        model_name=payload.model_name.strip(),
        base_url=payload.base_url.strip(),
        api_key=api_key,
        temperature=payload.temperature,
        max_steps=payload.max_steps if payload.max_steps and payload.max_steps >= 5 else 30,
        context_budget=payload.context_budget if payload.context_budget and payload.context_budget >= 4000 else 32000,
        max_context_turns=payload.max_context_turns if payload.max_context_turns and payload.max_context_turns >= 5 else 25,
    )
    bot_manager.save_llm_config(new_cfg)
    return {"success": True, "config": bot_manager.get_masked_llm_config()}


@router.post("/config/test")
def test_llm_connection(_user=Depends(require_admin)):
    """测试当前大模型连通性"""
    cfg = bot_manager.get_llm_config()
    try:
        model = agent_engine.create_model(cfg)
        resp = model([{"role": "user", "content": "Ping"}])
        text = str(resp) if hasattr(resp, "__str__") else "OK"
        return {"success": True, "message": "大模型连接成功！", "response": text[:100]}
    except Exception as e:
        return {"success": False, "error": f"连接失败: {str(e)}"}


def _validate_model_base_url(base_url: str) -> str:
    """允许公网模型端点与 Ollama 本地白名单端口，拒绝 SSRF 目标（委托共享守卫）。"""
    try:
        return model_url_guard.validate_model_base_url(base_url)
    except model_url_guard.UnsafeModelUrlError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _is_local_model_base_url(base_url: str) -> bool:
    return model_url_guard.is_local_model_base_url(base_url)


def _is_official_model_base_url(provider: str, base_url: str) -> bool:
    return model_url_guard.is_official_model_base_url(provider, base_url)


def _validate_model_override_for_persistence(model_override: Optional[Dict[str, Any]], user: CurrentUser) -> Optional[Dict[str, Any]]:
    """BOT 持久化前校验自定义模型地址：URL 安全 + 非管理员必须自带 API Key。"""
    if not model_override:
        return model_override
    base_url = str(model_override.get("base_url") or "").strip()
    if not base_url:
        return model_override
    _validate_model_base_url(base_url)
    provider = str(model_override.get("provider") or bot_manager.get_llm_config().provider or "").lower()
    trusted_target = _is_local_model_base_url(base_url) or _is_official_model_base_url(provider, base_url)
    if not user.is_admin and not trusted_target:
        api_key = str(model_override.get("api_key") or "").strip()
        if not api_key or "..." in api_key:
            raise HTTPException(status_code=403, detail="普通用户使用自定义模型地址时必须在 BOT 模型配置中提供自己的 API Key")
    return model_override


@router.post("/models")
@router.get("/models")
def fetch_available_models(payload: Optional[FetchModelsPayload] = None, user: CurrentUser = Depends(current_user)):
    """
    通过目标提供商的 OpenAI 兼容 /models 接口或 Ollama /api/tags 动态获取可用模型标识列表。
    若远程探测失败或未配置 Key，则智能返回常见高频预设模型作为候选，保留完全自定义能力。
    """
    cfg = bot_manager.get_llm_config()
    provider = (payload.provider if payload and payload.provider else cfg.provider) or "deepseek"

    if payload and payload.base_url is not None:
        base_url = payload.base_url.strip()
    else:
        base_url = (cfg.base_url or "").strip()

    # 获取有效 API Key (避免使用带 ... 的脱敏 Key)
    req_key = payload.api_key.strip() if payload and payload.api_key is not None else ""
    if req_key and "..." not in req_key:
        api_key = req_key
    elif payload and payload.provider and payload.provider != cfg.provider and not req_key:
        api_key = ""
    else:
        api_key = cfg.api_key

    # 常见提供商官方推荐预设候选池
    PROVIDER_PRESETS = {
        "deepseek": ["deepseek-chat", "deepseek-reasoner", "deepseek-flash", "deepseek-v4-pro"],
        "qwen": ["qwen-plus", "qwen-max", "qwen-turbo", "qwen-long", "qwen2.5-coder-32b-instruct", "qwen2.5-72b-instruct"],
        "ollama": ["qwen2.5-coder:7b", "qwen2.5:7b", "llama3.1:8b", "deepseek-r1:8b"],
        "openai": ["gpt-4o", "gpt-4o-mini", "o1", "o3-mini", "gpt-4-turbo"],
        "custom": []
    }

    is_official_target = not base_url or _is_official_model_base_url(provider, base_url)
    is_official_or_local = is_official_target or _is_local_model_base_url(base_url)
    if not user.is_admin and base_url and not is_official_or_local and not req_key:
        return {
            "success": True,
            "models": PROVIDER_PRESETS.get(provider, []),
            "source": "preset",
            "message": "普通用户探测自定义地址必须提供自己的 API Key，已返回推荐模型列表",
        }

    if not base_url:
        presets = PROVIDER_PRESETS.get(provider, [])
        return {
            "success": True,
            "models": presets,
            "source": "preset",
            "message": "Base URL 为空，返回该提供商默认推荐模型列表"
        }

    # Official host names still need the same DNS/IP policy check.  The
    # provider allowlist only identifies the intended service; it must not
    # bypass validation when DNS or a local proxy resolves that name to an
    # internal address.
    norm_base = _validate_model_base_url(base_url)
    candidates = []
    if provider == "ollama" or "11434" in norm_base:
        candidates.append(f"{norm_base}/api/tags")
        candidates.append(f"{norm_base}/v1/models")
        candidates.append(f"{norm_base}/models")
    else:
        candidates.append(f"{norm_base}/models")
        if not norm_base.endswith("/v1"):
            candidates.append(f"{norm_base}/v1/models")
        else:
            candidates.append(f"{norm_base[:-3]}/models")

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    found_models = []
    last_error = None

    for url in candidates:
        try:
            resp = requests.get(
                url,
                headers=headers,
                timeout=(3, 6),
                allow_redirects=False,
            )
            if resp.status_code == 200:
                data = resp.json()
                items = []
                if isinstance(data, dict):
                    if "data" in data and isinstance(data["data"], list):
                        items = data["data"]
                    elif "models" in data and isinstance(data["models"], list):
                        items = data["models"]
                elif isinstance(data, list):
                    items = data

                for item in items:
                    if isinstance(item, dict):
                        m_id = item.get("id") or item.get("name") or item.get("model")
                        if m_id and isinstance(m_id, str):
                            found_models.append(m_id)
                    elif isinstance(item, str):
                        found_models.append(item)

                if found_models:
                    break
            else:
                last_error = f"HTTP {resp.status_code}: {resp.text[:100]}"
        except Exception as e:
            last_error = str(e)

    if found_models:
        seen = set()
        deduped = []
        for m in found_models:
            if m not in seen:
                seen.add(m)
                deduped.append(m)
        return {
            "success": True,
            "models": deduped,
            "source": "remote",
            "message": f"成功从远程探测到 {len(deduped)} 个可用模型"
        }

    presets = PROVIDER_PRESETS.get(provider, [])
    return {
        "success": False,
        "models": presets,
        "source": "fallback",
        "error": last_error or "未探测到模型列表",
        "message": f"远程探测未响应（{last_error or '接口无返回'}），已呈现官方推荐常用模型"
    }


# -------------------------------------------------------------
# 智能体实例管理
# -------------------------------------------------------------
@router.get("/bots")
def list_bots(user: CurrentUser = Depends(current_user)):
    """获取当前用户可见的智能体实例列表"""
    bots = bot_manager.list_bots(user_id=user.subject)
    if not user.is_admin:
        bots = [bot for bot in bots if bot.get("visibility") == "public" or bot.get("owner_id") == user.subject]
    return {"bots": bots}


@router.post("/bots/generate-preview")
def generate_bot_preview(payload: GeneratePreviewPayload):
    """根据智能体名称与目标，智能预生成人设、图标与 ID"""
    profile = bot_manager.generate_bot_profile(payload.name, payload.goal or "")
    return {"success": True, "profile": profile}


@router.post("/bots")
def create_bot(payload: CreateBotPayload, user: CurrentUser = Depends(current_user)):
    """新建智能体（支持仅填名称与目标，AI 自动生成人设、ID 与个性化推荐指令）"""
    target_id = payload.id
    target_avatar = payload.avatar
    target_desc = payload.description
    target_prompt = payload.base_prompt
    target_suggested_prompts = payload.suggested_prompts

    # 若未提供 ID、提示词或推荐指令，自动触发 AI 规划与生成
    if not target_id or not target_prompt or not target_avatar or not target_suggested_prompts:
        profile = bot_manager.generate_bot_profile(payload.name, payload.goal or "")
        if not target_id:
            target_id = profile["id"]
        if not target_avatar:
            target_avatar = profile["avatar"]
        if not target_desc:
            target_desc = profile["description"]
        if not target_prompt:
            target_prompt = profile["base_prompt"]
        if not target_suggested_prompts:
            target_suggested_prompts = profile.get("suggested_prompts", [])

    if not target_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(target_id)):
        raise HTTPException(status_code=422, detail="BOT ID 只能包含字母、数字、下划线和连字符，长度为 1-64")

    # 持久化前强制校验自定义模型地址（SSRF 守卫 + 非管理员自带 Key）
    _validate_model_override_for_persistence(payload.model_override, user)
    requested_id = str(target_id)
    bot = None
    for attempt in range(8):
        if attempt == 0:
            candidate_id = requested_id
        else:
            suffix = uuid.uuid4().hex[:8]
            candidate_id = f"{requested_id[:64 - len(suffix) - 1]}_{suffix}"
        if bot_manager.get_bot(candidate_id):
            continue
        try:
            bot = bot_manager.create_bot(
                bot_id=candidate_id,
                name=payload.name,
                avatar=target_avatar or "🤖",
                description=target_desc or "",
                base_prompt=target_prompt,
                suggested_prompts=target_suggested_prompts,
                model_override=payload.model_override,
                owner_id=None if user.claims.get("mode") == "disabled" else user.subject,
                issuer=user.issuer,
                tenant_id=user.tenant_id,
            )
            break
        except BotIdConflict:
            continue
    if bot is None:
        raise HTTPException(status_code=409, detail="BOT ID 创建冲突，请稍后重试")
    return {"success": True, "bot": _format_bot_response(bot)}


def _format_bot_response(bot):
    """格式化智能体返回体，包含记忆、技能与实时合成的系统提示词以及四层分级记忆运行期透视"""
    data = bot.model_dump()
    data["skills"] = [s.model_dump() for s in bot_manager.list_skills(bot.id)]
    data["memories"] = [m.model_dump() for m in bot_manager.list_memories(bot.id)]
    data["history_count"] = len(bot_manager.get_history(bot.id))
    data["assembled_prompt"] = agent_engine.assemble_system_prompt(bot)
    if not data.get("suggested_prompts"):
        data["suggested_prompts"] = bot_manager.infer_suggested_prompts(bot.name, bot.description, bot.base_prompt)

    # 注入四层分级记忆运行期透视全景 (Hierarchical Memory Blueprint)
    try:
        from core.agent.memory import memory_manager
        llm_cfg = bot_manager.get_llm_config()
        effective_budget = getattr(bot, "context_budget", None) or getattr(llm_cfg, "context_budget", None) or 32000
        effective_turns = getattr(bot, "max_context_turns", None) or getattr(llm_cfg, "max_context_turns", None) or 25
        history = bot_manager.get_history(bot.id, limit=500)
        saved_state = agent_engine._get_sandbox(agent_engine._sandbox_key(bot.id))
        uploads_dir = bot_manager._uploads_dir(bot.id)

        ctx = memory_manager.build_hierarchical_context(
            bot_id=bot.id,
            bot=bot,
            user_message="[实时全量透视示例：当前会话状态探查]",
            all_history=history,
            saved_state=saved_state,
            attachments=[],
            system_prompt=data["assembled_prompt"],
            manager=bot_manager,
            uploads_dir=uploads_dir,
            context_budget=effective_budget,
            max_context_turns=effective_turns
        )

        working_limit = effective_turns
        recent_slice = history[-working_limit:] if len(history) > working_limit else history
        dialogue_slice = recent_slice[:-1] if (dialogue_slice := recent_slice) and len(recent_slice) > 1 else recent_slice
        working_turns_texts = memory_manager.build_working_memory(dialogue_slice, char_budget=effective_budget)
        working_chars = sum(len(t) for t in working_turns_texts)

        data["hierarchical_memory"] = {
            "working_memory_turns": ctx.working_memory_turns,
            "working_memory_chars": working_chars,
            "context_budget": effective_budget,
            "max_context_turns": effective_turns,
            "compacted_history_count": ctx.compacted_history_count,
            "running_summary": ctx.running_summary,
            "entities_catalog": ctx.entities_catalog,
            "runtime_variables": ctx.runtime_variables,
            "total_history_count": len(history),
            "assembled_task_prompt": ctx.task_prompt
        }
    except Exception as e:
        logger.warning("Failed to build hierarchical memory preview for bot %s: %s", bot.id, str(e))
        data["hierarchical_memory"] = None

    return data


@router.get("/bots/{bot_id}", dependencies=[Depends(require_bot_access)])
def get_bot(bot_id: str):
    """获取智能体详情与全量资产统计"""
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="智能体不存在")
    return {"bot": _format_bot_response(bot)}


@router.post("/bots/{bot_id}/suggested-prompts", dependencies=[Depends(require_bot_access)])
def refresh_bot_suggested_prompts(bot_id: str):
    """根据智能体人设，通过大模型动态重新生成推荐指令（换一换）"""
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="智能体不存在")
    prompts = bot_manager.generate_bot_suggested_prompts(bot_id, force_refresh=True)
    return {"success": True, "suggested_prompts": prompts}


@router.post("/bots/{bot_id}/ownership", dependencies=[Depends(require_scoped_admin)])
def transfer_bot_ownership(
    bot_id: str,
    payload: TransferBotOwnershipPayload,
    user: CurrentUser = Depends(current_user),
):
    """管理员显式认领/转移历史 ownerless 私有 Bot；不根据请求者自动猜测归属。"""
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="BOT 不存在")
    if bot.visibility == "public":
        raise HTTPException(status_code=400, detail="公共 BOT 无需设置 owner")
    bot.owner_id = payload.owner_id.strip()
    bot_manager.save_bot(bot)
    _write_agent_audit(
        user,
        action="agent.bot.ownership_transfer",
        bot_id=bot_id,
        metadata={"owner_subject": bot.owner_id},
    )
    return {"success": True, "bot": _format_bot_response(bot)}


@router.post("/bots/{bot_id}/legacy-state/migrate", dependencies=[Depends(require_scoped_admin)])
def migrate_bot_legacy_state(
    bot_id: str,
    payload: LegacyStateMigrationPayload,
    user: CurrentUser = Depends(current_user),
):
    """管理员将无法自动归属的认证前 BOT 状态迁移给明确 subject。"""
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="BOT 不存在")
    try:
        bot_manager.migrate_legacy_state(bot_id, payload.target_user_id.strip())
    except ValueError as exc:
        _write_agent_audit(
            user,
            action="agent.bot.legacy_state_migrate",
            bot_id=bot_id,
            outcome="REJECTED",
            metadata={"target_user_id": payload.target_user_id.strip(), "reason": str(exc)[:256]},
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _write_agent_audit(
        user,
        action="agent.bot.legacy_state_migrate",
        bot_id=bot_id,
        metadata={"target_user_id": payload.target_user_id.strip()},
    )
    return {
        "success": True,
        "bot_id": bot_id,
        "target_user_id": payload.target_user_id.strip(),
    }


@router.put("/bots/{bot_id}", dependencies=[Depends(require_bot_definition_editor)])
def update_bot(bot_id: str, payload: UpdateBotPayload, user: CurrentUser = Depends(current_user)):
    """更新智能体设定"""
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="智能体不存在")

    # 内置智能体保护：禁止修改基础人设、名称、头像与描述
    if bot_id == "default_assistant":
        if payload.base_prompt is not None and payload.base_prompt != bot.base_prompt:
            raise HTTPException(status_code=400, detail="内置智能体基础设定受系统保护，不可修改")
        if payload.name is not None and payload.name != bot.name:
            raise HTTPException(status_code=400, detail="内置智能体名称受系统保护，不可修改")
        if payload.avatar is not None and payload.avatar != bot.avatar:
            raise HTTPException(status_code=400, detail="内置智能体头像受系统保护，不可修改")
        if payload.description is not None and payload.description != bot.description:
            raise HTTPException(status_code=400, detail="内置智能体描述受系统保护，不可修改")

    if payload.name is not None:
        bot.name = payload.name
    if payload.avatar is not None:
        bot.avatar = payload.avatar
    if payload.description is not None:
        bot.description = payload.description
    if payload.base_prompt is not None:
        bot.base_prompt = payload.base_prompt
    if payload.model_override is not None:
        _validate_model_override_for_persistence(payload.model_override, user)
        bot.model_override = payload.model_override
    bot_manager.save_bot(bot)
    return {"success": True, "bot": _format_bot_response(bot)}


@router.delete("/bots/{bot_id}", dependencies=[Depends(require_bot_definition_editor)])
def delete_bot(bot_id: str):
    """删除智能体并彻底清理相关数据与运行任务"""
    if bot_id == "default_assistant":
        raise HTTPException(status_code=400, detail="默认智能体受系统内置保护，无法删除")
    agent_run_manager.stop_all_runs(bot_id)
    ok = bot_manager.delete_bot(bot_id)
    if not ok:
        raise HTTPException(status_code=404, detail="智能体不存在")
    return {"success": True}


# -------------------------------------------------------------
# 会话与 SSE 流式对话
# -------------------------------------------------------------
@router.get("/bots/{bot_id}/history", dependencies=[Depends(require_bot_access)])
def get_bot_history(
    bot_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    before_id: Optional[str] = Query(default=None),
    user: CurrentUser = Depends(current_user)
):
    """获取会话历史（支持根据 before_id 向上分页加载更早历史会话）"""
    return bot_manager.get_history_paged(bot_id, limit=limit, before_id=before_id, user_id=user.subject)


@router.delete("/bots/{bot_id}/history", dependencies=[Depends(require_bot_access)])
def clear_bot_history(bot_id: str, delete_files: bool = Query(default=True), user: CurrentUser = Depends(current_user)):
    """清空会话历史并同步清理物理磁盘上的上传附件"""
    bot_manager.clear_history(bot_id, delete_files=delete_files, user_id=user.subject)
    return {"success": True}


@router.post("/bots/{bot_id}/files/cleanup", dependencies=[Depends(require_bot_access)])
def cleanup_bot_files(bot_id: str, user: CurrentUser = Depends(current_user)):
    """主动扫描并清理未在历史消息中引用的孤儿文件"""
    try:
        deleted = bot_manager.cleanup_unreferenced_files(bot_id, user_id=user.subject)
    except Exception as exc:
        _write_agent_audit(
            user,
            action="agent.file.cleanup",
            bot_id=bot_id,
            outcome="FAILED",
            metadata={"error_type": type(exc).__name__},
        )
        raise
    _write_agent_audit(
        user,
        action="agent.file.cleanup",
        bot_id=bot_id,
        metadata={"deleted_count": int(deleted)},
    )
    return {"success": True, "cleaned_count": deleted}


def _stream_run_sse(run: AgentRun):
    """为指定 AgentRun 构造线程安全的 SSE 流式响应，支持断线重连与全量快照同步"""
    async def sse_event_generator():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_EVENTS_PER_RUN)
        sub, snapshot = run.register_subscriber(queue, loop)
        try:
            # 1. 立即下发当前全量快照，供前端初始化或断线同步
            sync_payload = json.dumps({"run": snapshot}, ensure_ascii=False)
            yield f"event: sync\ndata: {sync_payload}\n\n"

            # 如果任务在连接建立前已完成，则直接下发终止信号后退出
            if snapshot.get("status") in ("done", "error", "stopped"):
                evt_name = "error" if snapshot.get("status") == "error" else "done"
                yield f"event: {evt_name}\ndata: {sync_payload}\n\n"
                return

            # 2. 持续等待新事件产生并流式传输
            terminal_events = {"done", "error", "stopped"}
            terminal_status = {"done", "error", "stopped"}

            def render_event(event_obj: dict) -> tuple[str, str]:
                evt_name = event_obj.get("event", "message")
                data_str = json.dumps(event_obj.get("data", {}), ensure_ascii=False)
                return evt_name, f"event: {evt_name}\ndata: {data_str}\n\n"

            while True:
                try:
                    event_obj = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    if run.status not in terminal_status:
                        yield ": ping\n\n"
                        continue
                    while True:
                        try:
                            event_obj = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return
                        evt_name, payload = render_event(event_obj)
                        yield payload
                        if evt_name in terminal_events:
                            return
                else:
                    evt_name, payload = render_event(event_obj)
                    yield payload
                    if evt_name in terminal_events:
                        break
        finally:
            run.unregister_subscriber(sub)

    return StreamingResponse(
        sse_event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.get("/active-runs")
def list_all_active_runs(user: CurrentUser = Depends(current_user)):
    """查询当前用户活跃运行状态，管理员也只返回自己的运行状态。"""
    return {
        "active_runs": agent_run_manager.list_active_runs(
            user.subject,
            user.issuer,
            user.tenant_id,
        )
    }


@router.post("/bots/{bot_id}/upload", dependencies=[Depends(require_bot_access)])
async def upload_bot_file(
    bot_id: str,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(current_user),
):
    """
    上传文件/文档/图片附件给智能体处理
    支持 DOCX, XLSX/XLS, CSV, TXT, JSON, MD, LOG, PDF, 以及各类图片
    自动提取结构化数据摘要供大模型理解
    """
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="BOT 不存在")

    bot_manager.ensure_legacy_state(bot_id)
    bot_dir = bot_manager._runtime_dir(bot_id)
    uploads_dir = os.path.join(bot_dir, "uploads")
    try:
        os.makedirs(uploads_dir, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="无法创建文件上传目录") from exc
    original_filename = file.filename or "uploaded_file"
    safe_name = os.path.basename(original_filename)
    safe_name = re.sub(r"[^\w\.\-\u4e00-\u9fa5]", "_", safe_name)
    # Millisecond timestamps collide across two API processes.  The opaque
    # suffix keeps both the stored filename and the SQLite primary key unique
    # during concurrent single-host uploads.
    file_id = f"f_{int(time.time() * 1000)}_{uuid.uuid4().hex[:12]}"
    saved_name = f"{file_id}_{safe_name}"
    saved_path = os.path.abspath(os.path.join(uploads_dir, saved_name))

    content = await file.read(MAX_AGENT_UPLOAD_BYTES + 1)
    if len(content) > MAX_AGENT_UPLOAD_BYTES:
        _write_agent_audit(
            user,
            action="agent.file.upload",
            bot_id=bot_id,
            outcome="REJECTED",
            metadata={"file_id": file_id, "size_bytes": len(content), "reason": "size_limit"},
        )
        raise HTTPException(status_code=413, detail="上传文件超过 50 MiB 限制")

    bot_scope = bot_manager._metadata_scope(bot_id, user_id=user.subject, bot=bot)
    reserved = bot_manager.metadata_store.reserve_file_slot(
        reservation_id=file_id,
        bot_id=bot_id,
        max_files=MAX_AGENT_FILES_PER_BOT,
        **bot_scope,
    )
    if not reserved:
        _write_agent_audit(
            user,
            action="agent.file.upload",
            bot_id=bot_id,
            outcome="REJECTED",
            metadata={"file_id": file_id, "reason": "file_count_limit"},
        )
        raise HTTPException(status_code=413, detail="该智能体文件数量已达到上限，请先清理旧文件")

    try:
        with open(saved_path, "wb") as f:
            f.write(content)
    except OSError as exc:
        bot_manager.metadata_store.release_file_slot(file_id, **bot_scope)
        _write_agent_audit(
            user,
            action="agent.file.upload",
            bot_id=bot_id,
            outcome="FAILED",
            metadata={"file_id": file_id, "reason": "file_write", "error_type": type(exc).__name__},
        )
        raise HTTPException(status_code=500, detail="文件保存失败") from exc

    try:
        parsed = await run_in_threadpool(parse_file_summary, saved_path, safe_name)
    except Exception as exc:
        with suppress(OSError):
            os.remove(saved_path)
        bot_manager.metadata_store.release_file_slot(file_id, **bot_scope)
        _write_agent_audit(
            user,
            action="agent.file.upload",
            bot_id=bot_id,
            outcome="FAILED",
            metadata={"file_id": file_id, "reason": "parse", "error_type": type(exc).__name__},
        )
        raise HTTPException(status_code=422, detail="文件解析失败") from exc
    if parsed.get("error") == "pdf_page_limit":
        with suppress(OSError):
            os.remove(saved_path)
        bot_manager.metadata_store.release_file_slot(file_id, **bot_scope)
        _write_agent_audit(
            user,
            action="agent.file.upload",
            bot_id=bot_id,
            outcome="REJECTED",
            metadata={"file_id": file_id, "reason": "pdf_page_limit"},
        )
        raise HTTPException(status_code=422, detail=parsed.get("error_detail", "PDF 页数超过安全上限"))
    try:
        content_sha256 = hashlib.sha256(content).hexdigest()
        bot_manager.register_bot_file(
            bot_id,
            file_id=file_id,
            original_name=safe_name,
            stored_name=saved_name,
            content_type=file.content_type,
            size_bytes=len(content),
            sha256=content_sha256,
            user_id=user.subject,
        )
    except Exception as exc:
        with suppress(OSError):
            os.remove(saved_path)
        bot_manager.metadata_store.release_file_slot(file_id, **bot_scope)
        _write_agent_audit(
            user,
            action="agent.file.upload",
            bot_id=bot_id,
            outcome="FAILED",
            metadata={"file_id": file_id, "reason": "metadata", "error_type": type(exc).__name__},
        )
        raise HTTPException(status_code=500, detail="文件元数据写入失败") from exc
    _write_agent_audit(
        user,
        action="agent.file.upload",
        bot_id=bot_id,
        metadata={
            "file_id": file_id,
            "size_bytes": len(content),
            "sha256": content_sha256,
            "original_name": safe_name,
        },
    )
    file_url = f"/api/agent/bots/{bot_id}/files/{saved_name}"

    data_url = None
    if parsed["category"] == "image" and len(content) <= 3 * 1024 * 1024:
        mime = file.content_type or f"image/{parsed['type']}"
        b64 = base64.b64encode(content).decode("utf-8")
        data_url = f"data:{mime};base64,{b64}"

    return {
        "success": True,
        "file": {
            "id": file_id,
            "name": safe_name,
            "saved_name": saved_name,
            "size": len(content),
            "size_formatted": parsed["size_formatted"],
            "type": parsed["type"],
            "category": parsed["category"],
            "path": saved_path,
            "url": file_url,
            "data_url": data_url,
            "summary": parsed["summary"],
            "preview": parsed["preview"],
            "meta": parsed.get("meta", {})
        }
    }


@router.api_route("/files/{bot_id}/{filename}", methods=["GET", "HEAD"], dependencies=[Depends(require_bot_access)])
@router.api_route("/bots/{bot_id}/files/{filename}", methods=["GET", "HEAD"], dependencies=[Depends(require_bot_access)])
def get_bot_file(bot_id: str, filename: str, download: bool = Query(default=False)):
    """在线预览或下载智能体已上传的文件或生成的文件（支持原文件名或前缀哈希名双向匹配）"""
    bot_manager.ensure_legacy_state(bot_id)
    bot_dir = bot_manager._runtime_dir(bot_id)
    uploads_dir = os.path.join(bot_dir, "uploads")
    safe_filename = os.path.basename(filename)
    uploads_root = os.path.realpath(uploads_dir)

    def confined_file_path(name: str) -> str | None:
        """Resolve an upload member without allowing traversal or symlinks out."""
        candidate = os.path.realpath(os.path.join(uploads_root, os.path.basename(str(name))))
        try:
            if os.path.commonpath([uploads_root, candidate]) != uploads_root:
                return None
        except ValueError:
            return None
        return candidate if os.path.isfile(candidate) else None

    file_path = None
    bot_metadata = bot_manager.get_bot(bot_id)
    file_scope = bot_manager._metadata_scope(bot_id, bot=bot_metadata)
    metadata_record = bot_manager.metadata_store.find_file(
        bot_id,
        safe_filename,
        **file_scope,
    )
    if metadata_record:
        file_path = confined_file_path(str(metadata_record["stored_name"]))
        if file_path is None:
            raise HTTPException(status_code=404, detail="文件不存在或完整性校验失败")
        try:
            if int(metadata_record.get("size_bytes") or 0) != os.path.getsize(file_path):
                raise HTTPException(status_code=404, detail="文件不存在或完整性校验失败")
            expected_sha256 = str(metadata_record.get("sha256") or "")
            if expected_sha256:
                digest = hashlib.sha256()
                with open(file_path, "rb") as verified_file:
                    for chunk in iter(lambda: verified_file.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != expected_sha256:
                    raise HTTPException(status_code=404, detail="文件不存在或完整性校验失败")
        except OSError as exc:
            raise HTTPException(status_code=404, detail="文件不存在或完整性校验失败") from exc
    else:
        file_path = confined_file_path(safe_filename)

    # 1. 尝试直接路径匹配
    if file_path is None:
        candidate = None
        # 2. 从 BotManager 读取当前 JSONL/兼容历史，避免只看过时的
        # history.json 快照而漏掉新追加消息中的附件关联。
        try:
            history = bot_manager.get_history(bot_id, limit=5000)
            for msg in history:
                for att in msg.get("metadata", {}).get("attachments", []):
                    u = att.get("url", "")
                    sn = att.get("saved_name", "")
                    # 匹配 url 后缀或者原始文件名
                    if (u.endswith(f"/{safe_filename}") or att.get("name") == safe_filename) and sn:
                        sp = confined_file_path(sn)
                        if sp:
                            candidate = sp
                            break
                if candidate:
                    break
        except Exception:
            pass

        # 3. 尝试模糊/后缀回退匹配 (如传入 screenshot.png 匹配到 f_xxx_screenshot.png)
        if not candidate and os.path.exists(uploads_dir):
            matches = []
            try:
                directory_entries = os.listdir(uploads_dir)
            except OSError:
                directory_entries = []
            for fn in directory_entries:

                if fn == safe_filename or fn.endswith(f"_{safe_filename}"):
                    p = confined_file_path(fn)
                    if p:
                        matches.append((p, os.path.getmtime(p)))
            if matches:
                matches.sort(key=lambda x: x[1], reverse=True)
                candidate = matches[0][0]

        if candidate:
            file_path = candidate
        else:
            raise HTTPException(status_code=404, detail="文件不存在或已被移除")

    mime_type, _ = mimetypes.guess_type(file_path)
    # 图片、PDF、纯文本等格式支持浏览器直接预览
    active_mime = mime_type in {
        "text/html",
        "application/xhtml+xml",
        "application/javascript",
    }
    is_previewable = bool(
        mime_type and (
            mime_type.startswith("image/")
            or mime_type in ("application/pdf", "text/plain", "text/markdown", "application/json")
        )
    )
    # SVG is executable XML.  Sanitize it before allowing an inline response,
    # so Markdown <img> previews retain their MIME type without exposing the
    # same-origin page to scripts or external resource loads.  Invalid,
    # oversized, or otherwise unsafe SVGs keep the conservative download path.
    if mime_type == "image/svg+xml":
        try:
            with open(file_path, "rb") as svg_file:
                svg_content = svg_file.read(MAX_SAFE_SVG_BYTES + 1)
            sanitized_svg = sanitize_svg(svg_content)
        except (OSError, ValueError):
            disposition = "attachment"
            media_type = "application/octet-stream"
        else:
            disposition = "attachment" if download else "inline"
            safe_ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", safe_filename) or "image.svg"
            encoded_name = quote(safe_filename, safe="")
            response = Response(
                content=sanitized_svg,
                media_type="image/svg+xml",
                headers={
                    "Content-Disposition": f'{disposition}; filename="{safe_ascii_name}"; filename*=UTF-8\'\'{encoded_name}',
                    "Content-Security-Policy": "default-src 'none'; script-src 'none'; object-src 'none'; img-src 'data:'; base-uri 'none'; frame-ancestors 'none'",
                    "X-Content-Type-Options": "nosniff",
                    "Cache-Control": "no-store",
                },
            )
            return response
    else:
        disposition = "attachment" if download or active_mime else ("inline" if is_previewable else "attachment")
        media_type = "application/octet-stream" if active_mime else (mime_type or "application/octet-stream")

    response = FileResponse(
        file_path,
        media_type=media_type,
        content_disposition_type=disposition,
        filename=safe_filename
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@router.get("/bots/{bot_id}/files", dependencies=[Depends(require_bot_access)])
def list_bot_files(bot_id: str, user: CurrentUser = Depends(current_user)):
    """产出物与文件管理中心：获取智能体所有上传与生成的文件清单"""
    return {"files": bot_manager.list_bot_files(bot_id, user_id=user.subject)}


@router.delete("/bots/{bot_id}/files/{filename}", dependencies=[Depends(require_bot_access)])
def delete_bot_file(bot_id: str, filename: str, user: CurrentUser = Depends(current_user)):
    """删除智能体文件中心里的单个文件"""
    try:
        ok = bot_manager.delete_bot_file(bot_id, filename, user_id=user.subject)
    except Exception as exc:
        _write_agent_audit(
            user,
            action="agent.file.delete",
            bot_id=bot_id,
            outcome="FAILED",
            metadata={"filename": os.path.basename(filename), "error_type": type(exc).__name__},
        )
        raise
    if not ok:
        _write_agent_audit(
            user,
            action="agent.file.delete",
            bot_id=bot_id,
            outcome="REJECTED",
            metadata={"filename": os.path.basename(filename), "reason": "not_found"},
        )
        raise HTTPException(status_code=404, detail="文件不存在或删除失败")
    _write_agent_audit(
        user,
        action="agent.file.delete",
        bot_id=bot_id,
        metadata={"filename": os.path.basename(filename)},
    )
    return {"success": True}


def _unique_archive_name(clean_name: str, used_names: set[str]) -> str:
    base, ext = os.path.splitext(clean_name)
    arc_name = clean_name
    suffix = 1
    while arc_name in used_names:
        arc_name = f"{base} ({suffix}){ext}"
        suffix += 1
    used_names.add(arc_name)
    return arc_name


@router.get("/bots/{bot_id}/files-export", dependencies=[Depends(require_bot_access)])
def export_all_bot_files(
    bot_id: str,
    user: CurrentUser = Depends(current_user),
):
    """一键打包下载智能体所有产出物与文件（ZIP 压缩流）"""
    import tempfile
    import zipfile
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="BOT 不存在")

    bot_manager.ensure_legacy_state(bot_id)
    bot_dir = bot_manager._runtime_dir(bot_id)
    uploads_dir = os.path.join(bot_dir, "uploads")
    try:
        upload_names = os.listdir(uploads_dir) if os.path.exists(uploads_dir) else []
    except OSError as exc:
        raise HTTPException(status_code=500, detail="读取文件目录失败") from exc
    if not upload_names:
        raise HTTPException(status_code=404, detail="该智能体当前暂无任何产出物或文件")

    export_files: list[tuple[str, str, int]] = []
    total_bytes = 0
    for fn in sorted(upload_names):
        # Stop before touching any further directory entries once the count
        # quota is reached; the archive is intentionally a bounded prefix.
        if len(export_files) >= MAX_AGENT_FILES_PER_BOT:
            break
        fp = os.path.join(uploads_dir, fn)
        try:
            # Never follow a symlink from the asset directory during export.
            if os.path.islink(fp) or not os.path.isfile(fp):
                continue
            size = os.path.getsize(fp)
        except OSError as exc:
            raise HTTPException(status_code=500, detail="读取待导出文件失败") from exc
        if total_bytes + size > MAX_AGENT_EXPORT_BYTES:
            # Export a bounded prefix so a full asset directory remains
            # recoverable even after it crosses the aggregate budget.  The
            # archive-size check below still protects the response itself.
            break
        export_files.append((fn, fp, size))
        total_bytes += size
    if not export_files:
        _write_agent_audit(
            user,
            action="agent.file.export",
            bot_id=bot_id,
            outcome="REJECTED",
            metadata={"reason": "no_exportable_files"},
        )
        raise HTTPException(status_code=404, detail="该智能体当前暂无可导出的普通文件")

    # Build the ZIP on disk so a large archive does not occupy one contiguous
    # in-process BytesIO allocation.  The response generator removes the
    # temporary file after the client has consumed (or abandoned) the stream.
    tmp = tempfile.NamedTemporaryFile(prefix="tim_files_export_", suffix=".zip", delete=False)
    tmp_path = tmp.name
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            used_names = set()
            for fn, fp, _size in export_files:
                clean_name = re.sub(r"^f_\d+_", "", fn)
                arc_name = _unique_archive_name(clean_name, used_names)
                zf.write(fp, arcname=arc_name)
                if tmp.tell() > MAX_AGENT_EXPORT_BYTES:
                    raise HTTPException(status_code=413, detail="导出文件压缩结果超过 100 MiB 安全上限")
        # The central directory is written when ZipFile closes; include it in
        # the same limit check.
        tmp.flush()
        if os.path.getsize(tmp_path) > MAX_AGENT_EXPORT_BYTES:
            raise HTTPException(status_code=413, detail="导出文件压缩结果超过 100 MiB 安全上限")
    except Exception:
        with suppress(OSError):
            tmp.close()
        with suppress(OSError):
            os.remove(tmp_path)
        _write_agent_audit(
            user,
            action="agent.file.export",
            bot_id=bot_id,
            outcome="FAILED",
            metadata={"reason": "archive_build"},
        )
        raise
    finally:
        with suppress(OSError):
            tmp.close()

    import urllib.parse
    ascii_filename = f"{bot_id}_files_{int(time.time())}.zip"
    encoded_filename = urllib.parse.quote(f"{bot.name}_files_{int(time.time())}.zip")
    def zip_stream():
        try:
            with open(tmp_path, "rb") as archive:
                while True:
                    chunk = archive.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
            _write_agent_audit(
                user,
                action="agent.file.export",
                bot_id=bot_id,
                metadata={
                    "file_count": len(export_files),
                    "size_bytes": total_bytes,
                },
            )
        except Exception as exc:
            _write_agent_audit(
                user,
                action="agent.file.export",
                bot_id=bot_id,
                outcome="FAILED",
                metadata={"error_type": type(exc).__name__},
            )
            raise
        finally:
            with suppress(OSError):
                os.remove(tmp_path)

    return StreamingResponse(
        zip_stream(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{ascii_filename}"; filename*=utf-8\'\'{encoded_filename}'
        }
    )


@router.get("/bots/{bot_id}/active-run", dependencies=[Depends(require_bot_access)])
def get_bot_active_run(bot_id: str, user: CurrentUser = Depends(current_user)):
    """查询指定智能体当前是否有正在进行的会话任务（用于页面刷新状态恢复）"""
    run = agent_run_manager.get_active_run(
        bot_id,
        user.subject,
        user.issuer,
        user.tenant_id,
    )
    if not run:
        return {"has_active_run": False, "run": None}
    return {"has_active_run": True, "run": run.to_dict()}


@router.post("/bots/{bot_id}/chat", dependencies=[Depends(require_bot_access)])
async def chat_with_bot_stream(bot_id: str, payload: ChatPayload, user: CurrentUser = Depends(current_user)):
    """
    与指定 BOT 进行流式对话 (后台解耦执行，支持断线恢复与刷新重连)
    """
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="BOT 不存在")

    # 启动/复用后台执行任务
    raw_attachments = []
    for attachment in payload.attachments or []:
        item = attachment.model_dump()
        # The client-provided absolute path is untrusted.  ReadFileTool resolves
        # the basename inside the server-owned uploads directory.
        item["path"] = ""
        item["data_url"] = None
        item["meta"] = None
        raw_attachments.append(item)
    raw_attachments = raw_attachments or None
    run = agent_run_manager.start_run(
        bot_id=bot_id,
        user_message=payload.message,
        engine=agent_engine,
        bot_manager=bot_manager,
        client_msg_id=payload.client_msg_id,
        attachments=raw_attachments,
        user_id=user.subject,
        is_admin=user.is_admin,
        is_operator=user.is_operator,
        issuer=user.issuer,
        tenant_id=user.tenant_id,
    )
    return _stream_run_sse(run)


@router.post("/bots/{bot_id}/regenerate", dependencies=[Depends(require_bot_access)])
async def regenerate_bot_stream(bot_id: str, user: CurrentUser = Depends(current_user)):
    """
    重新生成最后一条助手回复：
    弹出上一次助手回答，复用上一条用户 Prompt 及附件重新发起推理执行流
    """
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="BOT 不存在")

    last_user = bot_manager.get_last_user_message(bot_id, user_id=user.subject)
    if not last_user:
        raise HTTPException(status_code=400, detail="当前暂无用户历史消息，无法重新生成")

    # 弹出上一次的助手回答
    bot_manager.pop_last_assistant_message(bot_id, user_id=user.subject)

    attachments = last_user.get("metadata", {}).get("attachments")
    run = agent_run_manager.start_run(
        bot_id=bot_id,
        user_message=last_user.get("content", ""),
        engine=agent_engine,
        bot_manager=bot_manager,
        attachments=attachments,
        skip_user_append=True,
        user_id=user.subject,
        is_admin=user.is_admin,
        is_operator=user.is_operator,
        issuer=user.issuer,
        tenant_id=user.tenant_id,
    )
    return _stream_run_sse(run)


@router.post("/bots/{bot_id}/truncate-history", dependencies=[Depends(require_bot_access)])
def truncate_bot_history(bot_id: str, payload: TruncateHistoryPayload, user: CurrentUser = Depends(current_user)):
    """截断会话历史至指定消息 ID（保留该消息及之前内容，用于编辑重发前的回滚）"""
    ok = bot_manager.truncate_history(bot_id, payload.message_id, user_id=user.subject)
    if not ok:
        raise HTTPException(status_code=404, detail="未找到指定的消息 ID")
    return {"success": True}


@router.get("/bots/{bot_id}/chat/stream", dependencies=[Depends(require_bot_access)])
async def resume_chat_stream(bot_id: str, user: CurrentUser = Depends(current_user)):
    """断线恢复流：重连当前正在进行中的会话流"""
    run = agent_run_manager.get_active_run(
        bot_id,
        user.subject,
        user.issuer,
        user.tenant_id,
    )
    if not run:
        raise HTTPException(status_code=404, detail="当前无进行中的会话任务")
    return _stream_run_sse(run)


@router.post("/bots/{bot_id}/stop", dependencies=[Depends(require_bot_access)])
def stop_bot_chat(bot_id: str, user: CurrentUser = Depends(current_user)):
    """中断当前正在执行的会话任务"""
    stopped = agent_run_manager.stop_run(
        bot_id,
        user.subject,
        user.issuer,
        user.tenant_id,
    )
    return {"success": stopped}


@router.post("/bots/{bot_id}/approvals/{approval_id}", dependencies=[Depends(require_bot_access), Depends(require_operator)])
def resolve_bot_approval(bot_id: str, approval_id: str, payload: ApprovalDecisionPayload, user: CurrentUser = Depends(require_operator)):
    """为指定智能体高危破坏性操作提交人机协同授权决策 (approve 或 reject)"""
    ok = agent_run_manager.resolve_approval(
        bot_id,
        approval_id,
        payload.decision,
        user.subject,
        user.issuer,
        user.tenant_id,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="未找到对应的待审批高危操作或已超时关闭")
    return {"success": True, "approval_id": approval_id, "decision": payload.decision}


@router.get("/bots/{bot_id}/export", dependencies=[Depends(require_bot_access)])
def export_bot_chat(
    bot_id: str,
    format: str = Query(default="markdown"),
    preview: bool = Query(default=False),
    user: CurrentUser | None = Depends(current_user),
):
    """一键导出当前智能体会话排障记录（支持 Markdown 与自包含单页交互式 HTML）"""
    import re
    import html as html_lib
    import tempfile
    import urllib.parse

    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="智能体不存在")

    export_user_id = getattr(user, "subject", None)
    history = bot_manager.get_history(bot_id, limit=2000, user_id=export_user_id)
    if not history:
        raise HTTPException(status_code=400, detail="当前暂无会话排障记录可供导出")
    estimated_history_bytes = 0
    for message in history:
        estimated_history_bytes += len(str(message.get("content") or "").encode("utf-8"))
        try:
            estimated_history_bytes += len(
                json.dumps(message.get("metadata") or {}, ensure_ascii=False, default=str).encode("utf-8")
            )
        except (TypeError, ValueError):
            estimated_history_bytes = MAX_AGENT_EXPORT_BYTES + 1
        if estimated_history_bytes > MAX_AGENT_EXPORT_BYTES:
            raise HTTPException(status_code=413, detail="会话导出内容超过 100 MiB 安全上限")

    export_time = time.strftime("%Y-%m-%d %H:%M:%S")
    clean_bot_name = re.sub(r'[/\\:*?"<>|]', '_', bot.name)

    if format.lower() == "html":
        # 生成高保真、独立可离线查看的 HTML 排障报告
        body_spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")

        class _BodyWriter:
            def append(self, value: str) -> None:
                body_spool.write(value.encode("utf-8"))

        turns_html = _BodyWriter()
        for idx, msg in enumerate(history, 1):
            role = msg.get("role", "user")
            content = (msg.get("content") or "").strip()
            meta = msg.get("metadata", {}) or {}
            ts = msg.get("timestamp", "")

            if role == "user":
                atts_html = ""
                atts = meta.get("attachments", []) or []
                if atts:
                    items = "".join([
                        f"<li style='margin: 4px 0;'><span style='color: #0284c7;'>📎</span> <code>{html_lib.escape(str(a.get('name', '')))}</code> ({html_lib.escape(str(a.get('type', '')))}, {html_lib.escape(str(a.get('size_formatted', '')))})</li>"
                        for a in atts if isinstance(a, dict)
                    ])
                    atts_html = f"<div style='margin-top: 10px; font-size: 12px; color: #64748b;'><strong>附件:</strong><ul style='padding-left: 20px; margin: 4px 0;'>{items}</ul></div>"

                turns_html.append(f"""
                <div style="margin-bottom: 24px; padding: 18px 22px; border-radius: 12px; background: #f8fafc; border: 1px solid #e2e8f0;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                        <span style="font-weight: 700; color: #0f172a; font-size: 14px;">👤 用户需求 [#{idx}]</span>
                        <span style="font-size: 11px; color: #94a3b8; font-family: monospace;">{html_lib.escape(str(ts))}</span>
                    </div>
                    <div class="markdown-content" style="font-size: 14px; line-height: 1.6; color: #334155; white-space: pre-wrap;">{html_lib.escape(content)}</div>
                    {atts_html}
                </div>
                """)
            else:
                thought = meta.get("thought", "") or ""
                thought_section = ""
                if thought:
                    thought_section = f"""
                    <details style="margin-bottom: 14px; background: #f1f5f9; padding: 10px 14px; border-radius: 8px; font-size: 12px; color: #475569; border-left: 3px solid #0284c7;">
                        <summary style="cursor: pointer; font-weight: 600; color: #0369a1;">💭 推理思考过程 (Thinking)</summary>
                        <div style="margin-top: 8px; white-space: pre-wrap; font-family: monospace; line-height: 1.5; font-size: 11px;">{html_lib.escape(str(thought))}</div>
                    </details>
                    """

                code_blocks = meta.get("code_blocks", []) or []
                logs = meta.get("execution_logs", []) or []
                code_section = ""
                if code_blocks:
                    c_parts = []
                    for cb in code_blocks:
                        if not isinstance(cb, dict):
                            continue
                        step_num = cb.get("step", 1)
                        c_code = cb.get("code", "") or ""
                        matching_log = next((l for l in logs if isinstance(l, dict) and l.get("step") == step_num), None)
                        log_out = matching_log.get("output", "") if matching_log else ""
                        c_parts.append(f"""
                        <div style="margin-top: 10px; border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden;">
                            <div style="background: #0f172a; color: #94a3b8; font-size: 11px; font-family: monospace; padding: 6px 12px; display: flex; justify-content: space-between;">
                                <span>⚡ 沙箱 Python 执行 · 步骤 {step_num}</span>
                            </div>
                            <pre style="margin: 0; padding: 12px; background: #1e293b; color: #f8fafc; font-size: 12px; overflow-x: auto; font-family: monospace; line-height: 1.4;">{html_lib.escape(str(c_code))}</pre>
                            {f'<div style="background: #f8fafc; padding: 10px 12px; font-size: 11px; border-top: 1px solid #e2e8f0; font-family: monospace; color: #334155;"><strong style="color: #0284c7;">执行结果:</strong><div style="white-space: pre-wrap; margin-top: 4px;">{html_lib.escape(str(log_out))}</div></div>' if log_out else ''}
                        </div>
                        """)
                    code_section = "".join(c_parts)

                turns_html.append(f"""
                <div style="margin-bottom: 28px; padding: 20px 24px; border-radius: 12px; background: #ffffff; border: 1px solid #cbd5e1; box-shadow: 0 1px 4px rgba(0,0,0,0.05);">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid #f1f5f9;">
                        <span style="font-weight: 700; color: #0284c7; font-size: 14px;">🤖 {html_lib.escape(bot.name)} 反馈 [#{idx}]</span>
                        <span style="font-size: 11px; color: #94a3b8; font-family: monospace;">{html_lib.escape(str(ts))}</span>
                    </div>
                    {thought_section}
                    {code_section}
                    <div class="markdown-content" style="margin-top: 14px; font-size: 14px; line-height: 1.7; color: #1e293b; white-space: pre-wrap;">{html_lib.escape(content)}</div>
                </div>
                """)

        # Keep the document shell small and stream the potentially large body
        # from the spool in bounded chunks.
        body_content = "<!-- TIM_BODY_STREAM_MARKER -->"
        html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-hashes' 'sha256-MguIPR6qNR8D3B+eAlK+bIRTZe8t3wkOY4B/56Me9FU='; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'">
    <title>TIM 智能体排障分析报告 - {html_lib.escape(bot.name)}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: #f1f5f9;
            color: #0f172a;
            margin: 0;
            padding: 30px 20px;
        }}
        .container {{
            max-width: 920px;
            margin: 0 auto;
            background: #ffffff;
            border-radius: 16px;
            padding: 32px 36px;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.06);
            border: 1px solid #e2e8f0;
        }}
        .header-actions {{
            display: flex;
            gap: 8px;
            margin-top: 14px;
        }}
        .btn {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 6px 14px;
            border-radius: 8px;
            font-size: 12px;
            font-weight: 600;
            text-decoration: none;
            cursor: pointer;
            border: 1px solid #cbd5e1;
            background: #ffffff;
            color: #334155;
            transition: all 0.15s ease;
        }}
        .btn:hover {{
            background: #f8fafc;
            border-color: #94a3b8;
            color: #0f172a;
        }}
        .btn-primary {{
            background: #0284c7;
            color: #ffffff;
            border-color: #0284c7;
        }}
        .btn-primary:hover {{
            background: #0369a1;
            border-color: #0369a1;
        }}
        .markdown-rendered h1, .markdown-rendered h2, .markdown-rendered h3 {{
            margin-top: 18px;
            margin-bottom: 8px;
            font-weight: 700;
            color: #0f172a;
        }}
        .markdown-rendered h1 {{ font-size: 18px; border-bottom: 1px solid #e2e8f0; padding-bottom: 6px; }}
        .markdown-rendered h2 {{ font-size: 16px; border-bottom: 1px solid #f1f5f9; padding-bottom: 4px; }}
        .markdown-rendered h3 {{ font-size: 14px; }}
        .markdown-rendered p {{ margin: 8px 0; line-height: 1.6; }}
        .markdown-rendered table {{
            border-collapse: collapse;
            width: 100%;
            margin: 12px 0;
            font-size: 12px;
        }}
        .markdown-rendered th, .markdown-rendered td {{
            border: 1px solid #cbd5e1;
            padding: 8px 12px;
            text-align: left;
        }}
        .markdown-rendered th {{
            background: #f8fafc;
            font-weight: 600;
            color: #334155;
        }}
        .markdown-rendered blockquote {{
            margin: 12px 0;
            padding: 8px 16px;
            border-left: 4px solid #0284c7;
            background: #f0f9ff;
            color: #0369a1;
            border-radius: 0 8px 8px 0;
        }}
        .markdown-rendered code {{
            background: #f1f5f9;
            padding: 2px 6px;
            border-radius: 4px;
            font-family: monospace;
            font-size: 12px;
            color: #0f172a;
        }}
        .markdown-rendered ul, .markdown-rendered ol {{
            padding-left: 20px;
            margin: 8px 0;
        }}
        .markdown-rendered li {{
            margin: 4px 0;
        }}
        @media print {{
            body {{ background: #fff; padding: 0; }}
            .container {{ box-shadow: none; border: none; padding: 0; max-width: 100%; }}
            .header-actions {{ display: none !important; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <header style="margin-bottom: 28px; padding-bottom: 20px; border-bottom: 2px solid #0284c7;">
            <div style="display: flex; align-items: center; justify-content: space-between;">
                <div style="display: flex; align-items: center; gap: 12px;">
                    <span style="font-size: 32px;">{html_lib.escape(bot.avatar or '🤖')}</span>
                    <div>
                        <h1 style="margin: 0; font-size: 20px; font-weight: 800; color: #0f172a;">TIM 智能体排障分析报告</h1>
                        <p style="margin: 4px 0 0; font-size: 13px; color: #64748b;">
                            智能体: <strong>{html_lib.escape(bot.name)}</strong> ({html_lib.escape(bot.id)})
                        </p>
                    </div>
                </div>
                <div style="text-align: right; font-size: 11px; color: #94a3b8; font-family: monospace;">
                    <div>生成时间: {html_lib.escape(export_time)}</div>
                    <div>会话记录: {len(history)} 条</div>
                </div>
            </div>
            <div style="margin-top: 14px; font-size: 12px; color: #475569; background: #f8fafc; padding: 10px 14px; border-radius: 8px; border: 1px solid #e2e8f0;">
                <strong>人设与定位:</strong> {html_lib.escape(bot.description or (bot.base_prompt or '')[:140])}
            </div>
            <div class="header-actions">
                <button type="button" onclick="window.print()" class="btn btn-primary">
                    🖨️ 打印报告 / 另存为 PDF
                </button>
                <a href="?format=markdown" class="btn">
                    📥 下载为 Markdown (.md)
                </a>
            </div>
        </header>

        <main>
            {body_content}
        </main>

        <footer style="margin-top: 36px; padding-top: 16px; border-top: 1px solid #e2e8f0; text-align: center; font-size: 11px; color: #94a3b8;">
            本报告由 Tencent Cloud TIM Studio 智能体全流程自动化生成 · 包含全链路代码执行与沙箱结果记录
        </footer>
    </div>

    <!-- 会话内容保持 HTML 转义后的纯文本，避免在导出报告中重新激活不可信 Markdown。 -->
</body>
</html>"""

        html_prefix, html_suffix = html_doc.split(body_content, 1)
        html_prefix_bytes = html_prefix.encode("utf-8")
        html_suffix_bytes = html_suffix.encode("utf-8")
        if len(html_prefix_bytes) + body_spool.tell() + len(html_suffix_bytes) > MAX_AGENT_EXPORT_BYTES:
            body_spool.close()
            raise HTTPException(status_code=413, detail="会话导出内容超过 100 MiB 安全上限")

        body_spool.seek(0)

        ascii_name = f"tim_report_{bot_id}_{int(time.time())}.html"
        encoded_name = urllib.parse.quote(f"{clean_bot_name}_排障报告_{time.strftime('%Y%m%d_%H%M%S')}.html")
        disposition_type = "inline" if preview else "attachment"

        def html_stream():
            try:
                yield html_prefix_bytes
                while True:
                    chunk = body_spool.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
                yield html_suffix_bytes
            finally:
                body_spool.close()

        return StreamingResponse(
            html_stream(),
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Disposition": f'{disposition_type}; filename="{ascii_name}"; filename*=utf-8\'\'{encoded_name}',
                # Permit only the fixed print handler; report content remains
                # escaped and no arbitrary script source is allowed.
                "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-hashes' 'sha256-MguIPR6qNR8D3B+eAlK+bIRTZe8t3wkOY4B/56Me9FU='; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
            }
        )

    # 默认 Markdown 格式导出
    md_spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")

    class _MarkdownLines:
        def __init__(self, spool):
            self.spool = spool
            self.first = True

        def append(self, value: str) -> None:
            if not self.first:
                self.spool.write(b"\n")
            self.spool.write(str(value).encode("utf-8"))
            self.first = False

    lines = _MarkdownLines(md_spool)
    for initial_line in [
        f"# 📋 TIM 智能体排障分析报告 - {bot.name}",
        "",
        f"> **生成时间**: {export_time}  ",
        f"> **智能体**: {bot.avatar} **{bot.name}** (`{bot.id}`)  ",
        f"> **职责定位**: {bot.description or '腾讯云 IM 全能业务技术支持'}  ",
        f"> **累计会话**: 共计 {len(history)} 条排障与操作记录  ",
        "",
        "---",
        ""
    ]:
        lines.append(initial_line)

    for idx, msg in enumerate(history, 1):
        role = msg.get("role", "user")
        content = (msg.get("content") or "").strip()
        meta = msg.get("metadata", {}) or {}
        ts = msg.get("timestamp", "")

        if role == "user":
            lines.append(f"### 👤 用户需求 [#{idx}] ({ts})\n")
            lines.append(f"{content}\n")
            atts = meta.get("attachments", []) or []
            if atts:
                lines.append("**📎 上传附件列表:**")
                for a in atts:
                    if isinstance(a, dict):
                        lines.append(f"- `{a.get('name')}` ({a.get('type')}, 大小: {a.get('size_formatted', '')})")
                lines.append("")
        else:
            lines.append(f"### 🤖 智能体回复 [#{idx}] ({ts})\n")
            thought = meta.get("thought", "") or ""
            if thought:
                lines.append("<details>\n<summary>💭 展开查看思考链推理过程</summary>\n")
                lines.append(thought)
                lines.append("\n</details>\n")

            code_blocks = meta.get("code_blocks", []) or []
            logs = meta.get("execution_logs", []) or []
            if code_blocks:
                lines.append("#### ⚙️ 沙箱 Python 代码执行与日志\n")
                for cb in code_blocks:
                    if isinstance(cb, dict):
                        step_num = cb.get("step", 1)
                        c_code = cb.get("code", "")
                        lines.append(f"```python\n# 步骤 {step_num}\n{c_code}\n```\n")
                        matching_log = next((l for l in logs if isinstance(l, dict) and l.get("step") == step_num), None)
                        if matching_log:
                            lines.append(f"**执行结果 (步骤 {step_num}):**\n\n{matching_log.get('output', '')}\n")

            lines.append(f"#### 🎯 智能体结论\n\n{content}\n")
            lines.append("\n---\n")

    if md_spool.tell() > MAX_AGENT_EXPORT_BYTES:
        md_spool.close()
        raise HTTPException(status_code=413, detail="会话导出内容超过 100 MiB 安全上限")
    md_spool.seek(0)
    ascii_name = f"tim_report_{bot_id}_{int(time.time())}.md"
    encoded_name = urllib.parse.quote(f"{clean_bot_name}_排障报告_{time.strftime('%Y%m%d_%H%M%S')}.md")
    def markdown_stream():
        try:
            while True:
                chunk = md_spool.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            md_spool.close()

    return StreamingResponse(
        markdown_stream(),
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{ascii_name}"; filename*=utf-8\'\'{encoded_name}'
        }
    )


# -------------------------------------------------------------
# 长期记忆管理 (Memory)
# -------------------------------------------------------------
@router.get("/bots/{bot_id}/memory", dependencies=[Depends(require_bot_access)])
def list_bot_memories(bot_id: str):
    """获取指定 BOT 的长期记忆"""
    return {"memories": [m.model_dump() for m in bot_manager.list_memories(bot_id)]}


@router.post("/bots/{bot_id}/memory", dependencies=[Depends(require_bot_access)])
def add_or_update_memory(bot_id: str, payload: MemoryPayload):
    """保存或更新长期记忆"""
    item = bot_manager.save_memory(bot_id, payload.key, payload.content)
    return {"success": True, "memory": item.model_dump()}


@router.delete("/bots/{bot_id}/memory/{key}", dependencies=[Depends(require_bot_access)])
def delete_memory(bot_id: str, key: str):
    """删除长期记忆"""
    ok = bot_manager.delete_memory(bot_id, key)
    return {"success": ok}


# -------------------------------------------------------------
# 提示词自我增强管理 (Prompt Self-Enhancement)
# -------------------------------------------------------------
@router.get("/bots/{bot_id}/guidelines", dependencies=[Depends(require_bot_access)])
def list_bot_guidelines(bot_id: str):
    """获取指定 BOT 的提示词自我强化准则"""
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="BOT 不存在")
    return {"guidelines": [g.model_dump() for g in bot.learned_guidelines]}


@router.post("/bots/{bot_id}/guidelines", dependencies=[Depends(require_bot_definition_editor)])
def add_guideline(bot_id: str, payload: GuidelinePayload):
    """手动或代添加行为准则"""
    g = bot_manager.add_learned_guideline(bot_id, payload.instruction, payload.reason or "")
    if not g:
        raise HTTPException(status_code=404, detail="BOT 不存在")
    return {"success": True, "guideline": g.model_dump()}


@router.delete("/bots/{bot_id}/guidelines/{guideline_id}", dependencies=[Depends(require_bot_definition_editor)])
def delete_guideline(bot_id: str, guideline_id: str):
    """删除某条增强准则"""
    ok = bot_manager.remove_learned_guideline(bot_id, guideline_id)
    return {"success": ok}


# -------------------------------------------------------------
# 自编技能库管理 (Skills)
# -------------------------------------------------------------
@router.get("/bots/{bot_id}/skills", dependencies=[Depends(require_bot_access)])
def list_bot_skills(bot_id: str):
    """获取指定 BOT 沉淀的技能列表"""
    skills = bot_manager.list_skills(bot_id)
    return {"skills": [s.model_dump() for s in skills]}


@router.post("/bots/{bot_id}/skills", dependencies=[Depends(require_bot_definition_editor)])
def create_or_save_skill(bot_id: str, payload: SaveSkillPayload):
    """用户或前端一键将验证通过的脚本沉淀为专属技能"""
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="BOT 不存在")
    try:
        _validate_skill_ast(payload.python_code)
    except (SyntaxError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"技能代码预检失败: {exc}") from exc
    skill = bot_manager.save_skill(
        bot_id=bot_id,
        name=payload.name,
        description=payload.description,
        code=payload.python_code,
        input_schema=payload.input_schema
    )
    return {"success": True, "skill": skill.model_dump()}


@router.get("/bots/{bot_id}/skills/{name}", dependencies=[Depends(require_bot_access)])
def get_bot_skill(bot_id: str, name: str):
    """获取指定技能代码与入参"""
    skill = bot_manager.get_skill(bot_id, name)
    if not skill:
        raise HTTPException(status_code=404, detail="技能不存在")
    return {"skill": skill.model_dump()}


@router.post("/bots/{bot_id}/skills/{name}/run", dependencies=[Depends(require_bot_access), Depends(require_operator)])
def run_bot_skill(bot_id: str, name: str, payload: RunSkillPayload, _user=Depends(require_operator)):
    """手动测试运行某个技能"""
    tim_client = TimClient()
    from core.agent.tools import RunSkillTool
    # This standalone endpoint has no interactive AgentRun approval context.
    # High-risk cloud calls must therefore fail closed; normal calls remain usable.
    approved_paths = {
        path.strip().lower()
        for path in payload.approved_high_risk_paths
        if path and path.strip()
    }

    def approve_specific_high_risk_call(approval: dict) -> str:
        if not payload.confirm_high_risk:
            return "reject"
        api_path = str(approval.get("api_path", "")).strip()
        api_meta = resolve_api(api_path)
        accepted = matching_confirmation_paths(api_path, api_meta)
        accepted.update(
            str(item).strip().lower()
            for item in (approval.get("api_paths") or [])
            if str(item).strip()
        )
        accepted.update(
            str(approval.get(key, "")).strip().lower()
            for key in ("api_id", "canonical_action")
            if str(approval.get(key, "")).strip()
        )
        return "approve" if accepted & approved_paths else "reject"

    runner = RunSkillTool(
        bot_id,
        bot_manager,
        tim_client,
        approval_requester=approve_specific_high_risk_call,
    )
    res = runner.forward(name, payload.kwargs)
    return res


@router.delete("/bots/{bot_id}/skills/{name}", dependencies=[Depends(require_bot_definition_editor)])
def delete_bot_skill(bot_id: str, name: str):
    """删除技能"""
    ok = bot_manager.delete_skill(bot_id, name)
    return {"success": ok}


# -------------------------------------------------------------
# 企业知识库与运维手册挂载 (Lightweight RAG)
# -------------------------------------------------------------
@router.get("/bots/{bot_id}/knowledge/docs", dependencies=[Depends(require_bot_definition_editor)])
def list_bot_knowledge_docs(bot_id: str):
    """获取指定智能体挂载的所有企业知识库与手册文档"""
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="BOT 不存在")
    docs = rag_manager.list_documents(bot_id)
    return {"docs": docs}


@router.post("/bots/{bot_id}/knowledge/upload", dependencies=[Depends(require_bot_definition_editor)])
async def upload_bot_knowledge_doc(
    bot_id: str,
    file: UploadFile = File(...)
):
    """上传并挂载业务规范文档/排障手册（支持 MD、TXT、DOCX、PDF、JSON、CSV）"""
    bot = bot_manager.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="BOT 不存在")
    if bot.visibility == "public":
        raise HTTPException(
            status_code=403,
            detail="公共 BOT 不允许挂载企业私有知识库，请改用私有 BOT",
        )

    filename = file.filename or "unknown_doc.txt"
    extension = os.path.splitext(filename)[1].lower()
    if extension == ".xls":
        raise HTTPException(status_code=422, detail="不支持旧版 .xls 格式，请另存为 .xlsx 后重新上传")
    if extension == ".xlsx":
        raise HTTPException(status_code=422, detail="知识库暂不支持 Excel 文件，请转换为 CSV 或 PDF 后上传")
    content = await file.read(MAX_KNOWLEDGE_UPLOAD_BYTES + 1)
    if len(content) > MAX_KNOWLEDGE_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="知识库文档超过 20 MiB 限制")
    if not content:
        raise HTTPException(status_code=400, detail="上传文件内容为空")

    try:
        doc_meta = await run_in_threadpool(rag_manager.add_document, bot_id, filename, content)
    except Exception as exc:
        logger.warning("Knowledge document indexing failed for bot %s: %s", bot_id, exc)
        raise HTTPException(status_code=422, detail=f"知识库文档无法建立索引: {exc}") from exc
    return {
        "success": True,
        "message": f"文档《{filename}》已成功挂载并构建 RAG 索引（生成 {doc_meta.get('chunks_count')} 个检索切片）",
        "doc": doc_meta
    }


@router.put("/bots/{bot_id}/knowledge/docs/{doc_id}", dependencies=[Depends(require_bot_definition_editor)])
def update_bot_knowledge_doc(bot_id: str, doc_id: str, payload: ToggleKnowledgePayload):
    """启用或禁用某篇文档参与 RAG 语义检索"""
    ok = rag_manager.toggle_document(bot_id, doc_id, payload.enabled)
    if not ok:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"success": True}


@router.delete("/bots/{bot_id}/knowledge/docs/{doc_id}", dependencies=[Depends(require_bot_definition_editor)])
def delete_bot_knowledge_doc(bot_id: str, doc_id: str):
    """删除挂载的知识文档及其索引切片"""
    ok = rag_manager.delete_document(bot_id, doc_id)
    if not ok:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"success": True}


@router.post("/bots/{bot_id}/knowledge/search", dependencies=[Depends(require_bot_definition_editor)])
def test_search_bot_knowledge(bot_id: str, payload: SearchKnowledgePayload):
    """在线检索与召回调试：测试特定问题在知识库中的命中结果与匹配分数"""
    results = rag_manager.search(
        bot_id=bot_id,
        query=payload.query,
        top_k=payload.top_k or 4,
        min_score=payload.min_score or 0.05
    )
    return {
        "query": payload.query,
        "count": len(results),
        "results": results
    }


@router.get("/bots/{bot_id}/knowledge/docs/{doc_id}/download", dependencies=[Depends(require_bot_definition_editor)])
def download_knowledge_doc(bot_id: str, doc_id: str):
    """下载知识库原始文件"""
    path = rag_manager.get_document_file_path(bot_id, doc_id)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="文件不存在")
    doc = rag_manager.get_document(bot_id, doc_id)
    filename = doc.get("name") if doc else os.path.basename(path)
    return FileResponse(
        path=path,
        filename=filename,
        media_type="application/octet-stream"
    )


# -------------------------------------------------------------
# 多智能体协同网络拓扑与流转观测 (Agent Orchestration Topology)
# -------------------------------------------------------------
@router.get("/topology", dependencies=[Depends(require_admin)])
def get_orchestration_topology():
    """获取系统全局多智能体协同网络拓扑（节点、流转链路、调用频次与近期协同事件）"""
    topology = bot_manager.get_orchestration_topology()
    return topology


@router.get("/bots/{bot_id}/orchestrations", dependencies=[Depends(require_bot_access)])
def get_bot_orchestrations(bot_id: str, limit: int = Query(default=20, ge=1, le=100), user: CurrentUser = Depends(current_user)):
    """获取当前用户可见的协同调用流水；管理员可查看全局记录。"""
    all_events = bot_manager.get_orchestrations(limit=200, user_id=user.subject, is_admin=user.is_admin)
    related = [e for e in all_events if e.get("caller_bot_id") == bot_id or e.get("target_bot_id") == bot_id]
    return {"orchestrations": related[:limit]}

