"""Immutable, non-secret policy for the opt-in public-page capability."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WebAccessError(ValueError):
    """A bounded, user-visible failure without response bodies or credentials."""


def canonical_domain(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 253:
        raise ValueError("域名必须是不含协议、路径和空白的完整主机名")
    try:
        host = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("域名编码无效") from exc
    if len(host) > 253 or "." not in host or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
        for part in host.split(".")
    ):
        raise ValueError("仅接受完整域名，不支持通配符、IP 或单标签主机名")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("网页读取不接受 IP 字面量")
    if host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("网页读取不接受本地域名")
    return host


class WebReadPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    allowed_domains: tuple[str, ...] = ()
    allowed_ports: tuple[int, ...] = (443,)
    connect_timeout_seconds: int = Field(default=3, ge=1, le=10)
    total_timeout_seconds: int = Field(default=10, ge=2, le=30)
    max_response_bytes: int = Field(default=524_288, ge=1024, le=2_097_152)
    max_text_chars: int = Field(default=40_000, ge=100, le=100_000)
    max_concurrent_requests: int = Field(default=2, ge=1, le=8)
    requests_per_minute: int = Field(default=20, ge=1, le=120)

    @field_validator("allowed_domains", mode="before")
    @classmethod
    def domains(cls, value):
        if not isinstance(value, (list, tuple)) or len(value) > 64:
            raise ValueError("最多允许 64 个精确域名")
        result = tuple(canonical_domain(item) for item in value)
        if len(set(result)) != len(result):
            raise ValueError("允许域名不能重复")
        return result

    @field_validator("allowed_ports", mode="before")
    @classmethod
    def ports(cls, value):
        if not isinstance(value, (list, tuple)) or len(value) != 1 or type(value[0]) is not int or value[0] != 443:
            raise ValueError("首版网页读取仅支持 HTTPS 443 端口")
        return (443,)

    @model_validator(mode="after")
    def deadlines(self):
        if self.connect_timeout_seconds > self.total_timeout_seconds:
            raise ValueError("连接超时不能大于整体超时")
        return self


def validate_web_url(url: str, policy: WebReadPolicy) -> tuple[str, str, str]:
    if not isinstance(url, str) or not url or len(url) > 4096 or any(
        ord(char) < 33 or ord(char) == 127 for char in url
    ) or "\\" in url:
        raise WebAccessError("网页 URL 格式无效或超过长度限制")
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError
        if parsed.fragment or (parsed.port if parsed.port is not None else 443) not in policy.allowed_ports:
            raise ValueError
        host = canonical_domain(parsed.hostname)
        if host not in policy.allowed_domains:
            raise WebAccessError("目标域名未获管理员批准")
        # Reconstruct the authority once; the HTTP transport receives the host
        # and origin-form target separately and never parses a second URL.
        path = parsed.path or "/"
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError
        target = path + ("?" + parsed.query if parsed.query else "")
        target.encode("ascii")  # Require percent encoding for non-ASCII paths.
        return urlunsplit(("https", host, path, parsed.query, "")), host, target
    except WebAccessError:
        raise
    except (ValueError, UnicodeError) as exc:
        raise WebAccessError("仅允许批准域名的 HTTPS/443 URL，不接受账号密码、片段或 IP 地址") from exc


def public_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if "%" in value:
        raise WebAccessError("网页 DNS 不接受带区域标识的地址")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise WebAccessError("DNS 返回了无效地址") from exc
    # Reject embedded IPv4 and transition forms rather than relying on the
    # host's routing interpretation of otherwise global IPv6 addresses.
    if (not address.is_global or address.is_multicast or address.is_reserved
        or address == ipaddress.ip_address("168.63.129.16")) or (
        isinstance(address, ipaddress.IPv6Address) and (
            address.ipv4_mapped is not None or address.sixtofour is not None
            or address.teredo is not None or address in ipaddress.ip_network("64:ff9b::/96")
            or address in ipaddress.ip_network("64:ff9b:1::/48")
        )
    ):
        raise WebAccessError("禁止访问私网、环回、元数据、保留或转换地址")
    return address
