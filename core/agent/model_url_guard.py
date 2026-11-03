"""共享的模型出站地址 SSRF 守卫。

apis/agent.py 的 API 层校验与 core 层（engine / bot_manager）的实际出站请求
都必须经过同一份校验逻辑，避免"仅发现接口校验、执行路径绕过"的缺口。
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse, urlunparse

LOCAL_MODEL_ALLOWED_PORTS = {11434, 8080}
LOCAL_MODEL_HOSTS = {"localhost", "127.0.0.1", "::1"}
PRIVATE_MODEL_HOSTS_ENV = "ALLOW_PRIVATE_MODEL_HOSTS"

_OFFICIAL_MODEL_HOSTS = {
    "deepseek": {"api.deepseek.com"},
    "qwen": {"dashscope.aliyuncs.com"},
    "openai": {"api.openai.com"},
}


class UnsafeModelUrlError(ValueError):
    """模型 Base URL 指向被禁止的网络位置时抛出。"""


def _configured_private_model_hosts() -> tuple[set[str], tuple[ipaddress._BaseNetwork, ...]]:
    """读取显式信任的私有模型主机。

    The value is deliberately an exact host/IP (or CIDR) allowlist.  Wildcards
    are not supported because they would turn a typo in an internal DNS name
    into a broad SSRF exception.  The setting is read for every validation so
    deployments and tests can change it without a process restart.
    """
    hosts: set[str] = set()
    networks: list[ipaddress._BaseNetwork] = []
    raw_hosts = os.environ.get(PRIVATE_MODEL_HOSTS_ENV, "")
    for raw_host in raw_hosts.split(","):
        host = raw_host.strip().lower().rstrip(".")
        if not host:
            continue
        # Environment values are hosts, not URLs.  Permit bracketed IPv6 for
        # consistency with URL notation while rejecting host:port entries.
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        try:
            if "/" in host:
                networks.append(ipaddress.ip_network(host, strict=False))
            else:
                ipaddress.ip_address(host)
                hosts.add(host)
        except ValueError:
            # A non-IP value is an exact DNS name.  Invalid-looking values are
            # ignored rather than accidentally broadening the exception.
            if ":" not in host and "/" not in host and " " not in host:
                hosts.add(host)
    return hosts, tuple(networks)


def _is_private_network_address(address: ipaddress._BaseAddress) -> bool:
    """Return whether an address is a usable RFC1918/unique-local target."""
    return (
        address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_reserved
        and not address.is_multicast
        and not address.is_unspecified
    )


def _is_configured_private_host(host: str, addresses: set[ipaddress._BaseAddress]) -> bool:
    """Check an explicit host/CIDR allowlist without allowing public DNS."""
    configured_hosts, configured_networks = _configured_private_model_hosts()
    host_matches = host in configured_hosts
    if not host_matches:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        host_matches = literal is not None and any(literal in network for network in configured_networks)
    return host_matches and bool(addresses) and all(_is_private_network_address(address) for address in addresses)


def _local_model_allowed_ports() -> set[int]:
    """读取显式配置的本地模型端口白名单，默认不放宽。"""
    raw_ports = os.environ.get("LOCAL_MODEL_ALLOWED_PORTS", "")
    if not raw_ports.strip():
        return set(LOCAL_MODEL_ALLOWED_PORTS)
    ports: set[int] = set()
    for raw_port in raw_ports.split(","):
        try:
            port = int(raw_port.strip())
        except ValueError:
            continue
        if 1 <= port <= 65535:
            ports.add(port)
    return ports or set(LOCAL_MODEL_ALLOWED_PORTS)


def validate_model_base_url(base_url: str) -> str:
    """允许公网模型端点与 Ollama 本地白名单端口，拒绝 SSRF 目标。"""
    try:
        parsed = urlparse(base_url)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise UnsafeModelUrlError("Base URL 必须是有效的 http(s) 地址") from exc
    if parsed.scheme not in {"http", "https"} or not host:
        raise UnsafeModelUrlError("Base URL 必须是有效的 http(s) 地址")
    if parsed.username or parsed.password:
        raise UnsafeModelUrlError("Base URL 不得包含账号密码")

    host = host.lower().rstrip(".")
    if host in LOCAL_MODEL_HOSTS:
        # Ollama 默认 11434；未写端口时归一化补齐，显式端口必须命中白名单。
        if port is None:
            netloc = parsed.hostname
            if ":" in netloc and not netloc.startswith("["):
                netloc = f"[{netloc}]"
            return urlunparse(parsed._replace(netloc=f"{netloc}:11434")).rstrip("/")
        if port not in _local_model_allowed_ports():
            allowed_ports = ", ".join(str(item) for item in sorted(_local_model_allowed_ports()))
            raise UnsafeModelUrlError(
                f"本地模型端口不在允许列表，仅支持：{allowed_ports}；可通过 LOCAL_MODEL_ALLOWED_PORTS 配置"
            )
        return base_url.rstrip("/")

    try:
        literal_address = ipaddress.ip_address(host)
    except ValueError:
        literal_address = None

    try:
        addresses = {literal_address} if literal_address is not None else {
            ipaddress.ip_address(info[4][0])
            for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError) as exc:
        raise UnsafeModelUrlError("Base URL 域名无法安全解析") from exc

    if _is_configured_private_host(host, addresses):
        return base_url.rstrip("/")

    if not addresses or any(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        for address in addresses
    ):
        raise UnsafeModelUrlError("禁止访问内网、环回、链路本地或保留地址")
    return base_url.rstrip("/")


def is_local_model_base_url(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower().rstrip(".")
    configured_hosts, configured_networks = _configured_private_model_hosts()
    if host in configured_hosts or host in LOCAL_MODEL_HOSTS:
        return True
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(literal in network for network in configured_networks)


def is_official_model_base_url(provider: str, base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower().rstrip(".")
    return provider != "ollama" and host in _OFFICIAL_MODEL_HOSTS.get(provider, set())
