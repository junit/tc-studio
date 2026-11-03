"""Pluggable authentication primitives for local, OIDC, and Keycloak deployments."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, cast

import jwt
import requests
from fastapi import Depends, HTTPException, Request
from jwt.algorithms import RSAAlgorithm
from starlette.responses import JSONResponse

from core.runtime_context import runtime_is_admin, runtime_is_operator, runtime_user_id


@dataclass(frozen=True)
class CurrentUser:
    subject: str
    roles: frozenset[str] = field(default_factory=frozenset)
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles or "administrator" in self.roles

    @property
    def is_operator(self) -> bool:
        return self.is_admin or bool({"operator", "editor"} & self.roles)


class AuthSettings:
    def __init__(self) -> None:
        self.environment = os.getenv("APP_ENV", "development").lower()
        self.allow_insecure_local = os.getenv("ALLOW_INSECURE_LOCAL_AUTH", "false").lower() in {"1", "true", "yes", "on"}
        self.enabled = os.getenv("AUTH_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
        self.provider = os.getenv("AUTH_PROVIDER", "oidc").lower()
        self.issuer = os.getenv("OIDC_ISSUER_URL", "").rstrip("/")
        self.client_id = os.getenv("OIDC_CLIENT_ID", "").strip()
        self.audience = os.getenv("OIDC_AUDIENCE", "").strip() or self.client_id
        self.jwks_url = os.getenv("OIDC_JWKS_URL", "").strip()
        self.required_role = os.getenv("AUTH_REQUIRED_ROLE", "").strip()
        self.jwks_ttl = max(30, int(os.getenv("OIDC_JWKS_TTL_SECONDS", "300")))


# 仅这些环境允许在未启用认证时回落到本地管理员模式；其余环境一律 fail-closed。
LOCAL_DEVELOPMENT_ENVIRONMENTS = {"development", "dev", "local"}

_settings = AuthSettings()
_jwks_cache: tuple[float, dict[str, Any]] | None = None
_jwks_lock = threading.Lock()
_JWKS_FORCE_REFRESH_COOLDOWN = 10.0
_unknown_kid_cache: dict[str, float] = {}


def reload_auth_settings() -> AuthSettings:
    """Reload environment-backed settings; useful for tests and process startup."""
    global _settings, _unknown_kid_cache
    _settings = AuthSettings()
    with _jwks_lock:
        _unknown_kid_cache = {}
    return _settings


def _extract_roles(claims: dict[str, Any], client_id: str) -> frozenset[str]:
    roles: set[str] = set()
    for value in (claims.get("roles"), claims.get("groups")):
        if isinstance(value, list):
            roles.update(str(item).lower() for item in value)
    realm = claims.get("realm_access")
    if isinstance(realm, dict) and isinstance(realm.get("roles"), list):
        roles.update(str(item).lower() for item in realm["roles"])
    resources = claims.get("resource_access")
    if isinstance(resources, dict):
        client = resources.get(client_id) if client_id else None
        if isinstance(client, dict) and isinstance(client.get("roles"), list):
            roles.update(str(item).lower() for item in client["roles"])
    return frozenset(roles)


def _get_jwks(settings: AuthSettings, force: bool = False) -> dict[str, Any]:
    global _jwks_cache
    # Read the cache without holding the lock.  The request below can take
    # several seconds and must not serialize every authenticated request.
    initial_cache = _jwks_cache
    now = time.monotonic()
    if initial_cache:
        age = now - initial_cache[0]
        if force and age < _JWKS_FORCE_REFRESH_COOLDOWN:
            return initial_cache[1]
        if not force and age < settings.jwks_ttl:
            return initial_cache[1]

    url = settings.jwks_url or (f"{settings.issuer}/protocol/openid-connect/certs" if settings.issuer else "")
    if not url or not url.startswith("https://"):
        raise RuntimeError("OIDC_JWKS_URL 或 HTTPS OIDC_ISSUER_URL 未配置")

    # Double-check after acquiring the short-lived lock.  Only cache reads and
    # the eventual publication are protected; the network request stays out of
    # the critical section.
    with _jwks_lock:
        cached = _jwks_cache
        now = time.monotonic()
        if cached:
            age = now - cached[0]
            if force and age < _JWKS_FORCE_REFRESH_COOLDOWN:
                return cached[1]
            if not force and age < settings.jwks_ttl:
                return cached[1]

    response = requests.get(url, timeout=(3, 5), allow_redirects=False)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
        raise RuntimeError("OIDC JWKS 响应格式无效")

    # A concurrent fetch may have published a newer result while this request
    # was in flight.  Never overwrite that result with an older response.
    fetched_at = time.monotonic()
    initial_timestamp = initial_cache[0] if initial_cache else 0.0
    with _jwks_lock:
        current = _jwks_cache
        if current and current[0] > initial_timestamp:
            return current[1]
        _jwks_cache = (fetched_at, data)
        return data


def _decode_oidc_token(token: str, settings: AuthSettings) -> CurrentUser:
    if not settings.issuer or not settings.audience:
        raise RuntimeError("OIDC_ISSUER_URL 与 OIDC_AUDIENCE 必须配置")
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    keys = _get_jwks(settings).get("keys", [])
    jwk = next((item for item in keys if item.get("kid") == kid), None)
    if not jwk:
        # IdPs may rotate signing keys before the normal TTL expires. Refresh once on kid miss.
        now = time.monotonic()
        with _jwks_lock:
            last_unknown = _unknown_kid_cache.get(str(kid))
            if last_unknown is None or now - last_unknown >= _JWKS_FORCE_REFRESH_COOLDOWN:
                _unknown_kid_cache[str(kid)] = now
                refresh_allowed = True
            else:
                refresh_allowed = False
        if refresh_allowed:
            keys = _get_jwks(settings, force=True).get("keys", [])
            jwk = next((item for item in keys if item.get("kid") == kid), None)
    if not jwk:
        raise ValueError("OIDC signing key not found")
    public_key = RSAAlgorithm.from_jwk(json.dumps(jwk))
    options = {
        "verify_aud": bool(settings.audience),
        "require": ["exp", "iat", "sub"],
    }
    claims = jwt.decode(
        token,
        key=cast(Any, public_key),
        algorithms=["RS256"],
        issuer=settings.issuer or None,
        audience=settings.audience or None,
        options=options,
    )
    if not isinstance(claims, dict) or not claims.get("sub"):
        raise ValueError("OIDC token subject missing")
    roles = _extract_roles(claims, settings.client_id)
    if settings.required_role and settings.required_role not in roles:
        raise PermissionError("required role missing")
    return CurrentUser(subject=str(claims["sub"]), roles=roles, claims=claims)


def authenticate_request(request: Request) -> CurrentUser:
    settings = _settings
    if not settings.enabled:
        if settings.environment not in LOCAL_DEVELOPMENT_ENVIRONMENTS and not settings.allow_insecure_local:
            raise HTTPException(status_code=503, detail="非本地开发环境必须启用认证（AUTH_ENABLED=true），或明确设置 ALLOW_INSECURE_LOCAL_AUTH=true")
        return CurrentUser(subject="local", roles=frozenset({"admin", "operator"}), claims={"mode": "disabled"})
    if settings.provider != "oidc":
        raise HTTPException(status_code=503, detail="未支持的认证提供商")
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="需要 Bearer Token", headers={"WWW-Authenticate": "Bearer"})
    try:
        return _decode_oidc_token(token, settings)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="缺少所需角色") from exc
    except (jwt.PyJWTError, requests.RequestException, RuntimeError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=401, detail="无效或已过期的认证令牌") from exc


def current_user(request: Request) -> CurrentUser:
    cached = getattr(request.state, "current_user", None)
    if isinstance(cached, CurrentUser):
        return cached
    user = authenticate_request(request)
    request.state.current_user = user
    return user


def require_admin(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def require_operator(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    if not user.is_operator:
        raise HTTPException(status_code=403, detail="需要运维操作权限")
    return user


class AuthenticationMiddleware:
    """Protect API/SSE routes while keeping static assets and health probes public.

    This is a small ASGI middleware rather than ``BaseHTTPMiddleware``.  The
    latter returns as soon as streaming response headers are ready, which would
    reset the runtime context before the response generator emits its body.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        if request.method == "OPTIONS" or not request.url.path.startswith("/api/"):
            await self.app(scope, receive, send)
            return
        try:
            user = authenticate_request(request)
        except HTTPException as exc:
            response = JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers=exc.headers,
            )
            await response(scope, receive, send)
            return

        request.state.current_user = user
        token = runtime_user_id.set(user.subject)
        admin_token = runtime_is_admin.set(user.is_admin)
        operator_token = runtime_is_operator.set(user.is_operator)
        try:
            await self.app(scope, receive, send)
        finally:
            runtime_is_admin.reset(admin_token)
            runtime_is_operator.reset(operator_token)
            runtime_user_id.reset(token)
