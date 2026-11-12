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

from core.namespace import get_env
from core.runtime_context import (
    AuthScope,
    runtime_auth_scope,
    runtime_is_admin,
    runtime_is_operator,
    runtime_user_id,
)


@dataclass(frozen=True)
class CurrentUser:
    subject: str
    roles: frozenset[str] = field(default_factory=frozenset)
    claims: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def _claim_text(value: Any) -> str:
        """Return a bounded scalar claim value suitable for persistence keys."""
        if isinstance(value, (dict, list, tuple, set)):
            return ""
        text = str(value or "").strip()
        if not text:
            return ""
        return text[:512]

    @property
    def issuer(self) -> str:
        """The signed OIDC issuer used as part of every tenant data scope.

        Local development users intentionally have an empty issuer.  An OIDC
        subject alone is not a globally unique tenant boundary: the same
        subject can exist under two issuers, so persistence callers must carry
        both values.
        """
        return self._claim_text(self.claims.get("iss")).rstrip("/")

    @property
    def tenant_id(self) -> str:
        """Resolve the deployment's supported tenant claim aliases.

        The value is trusted only because it came from the verified token.  A
        missing tenant claim remains empty for single-tenant deployments; it
        does not silently inherit another user's tenant.
        """
        for claim_name in ("tenant_id", "tenant", "tid", "org_id"):
            value = self._claim_text(self.claims.get(claim_name))
            if value:
                return value
        return ""

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles or "administrator" in self.roles

    @property
    def is_operator(self) -> bool:
        return self.is_admin or bool({"operator", "editor"} & self.roles)

    @property
    def auth_scope(self) -> AuthScope:
        return AuthScope(
            issuer=self.issuer,
            tenant_id=self.tenant_id,
            subject=self.subject,
        )


class AuthSettings:
    def __init__(self) -> None:
        # A missing deployment environment must fail closed.  Local callers
        # should opt in explicitly with TC_STUDIO_APP_ENV=development (and
        # tests do so in tests/conftest.py); silently treating an unset value as a local
        # administrator is unsafe for container and process managers.
        raw_environment = get_env("APP_ENV")
        self.environment = (raw_environment or "").strip().lower()
        self.app_env_configured = bool(raw_environment and raw_environment.strip())
        self.allow_insecure_local = (get_env("ALLOW_INSECURE_LOCAL_AUTH", default="false") or "").lower() in {"1", "true", "yes", "on"}
        raw_enabled = get_env("AUTH_ENABLED")
        self.auth_enabled_configured = bool(raw_enabled and raw_enabled.strip())
        self.enabled = (raw_enabled or "false").lower() in {"1", "true", "yes", "on"}
        self.provider = (get_env("AUTH_PROVIDER", default="oidc") or "oidc").lower()
        self.issuer = os.getenv("OIDC_ISSUER_URL", "").rstrip("/")
        self.client_id = os.getenv("OIDC_CLIENT_ID", "").strip()
        self.audience = os.getenv("OIDC_AUDIENCE", "").strip() or self.client_id
        self.jwks_url = os.getenv("OIDC_JWKS_URL", "").strip()
        self.required_role = get_env("AUTH_REQUIRED_ROLE", default="") or ""
        self.required_role = self.required_role.strip()
        raw_jwks_ttl = get_env("OIDC_JWKS_TTL_SECONDS", default="300") or "300"
        try:
            self.jwks_ttl = max(30, int(raw_jwks_ttl))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("OIDC_JWKS_TTL_SECONDS 必须是整数") from exc


# 仅这些环境允许在未启用认证时回落到本地管理员模式；其余环境一律 fail-closed。
LOCAL_DEVELOPMENT_ENVIRONMENTS = {"development", "dev", "local"}

_settings = AuthSettings()
_jwks_cache: tuple[float, dict[str, Any]] | None = None
_jwks_lock = threading.Lock()
_jwks_refresh_state: "_JWKSRefreshState | None" = None
_JWKS_FORCE_REFRESH_COOLDOWN = 10.0
_UNKNOWN_KID_CACHE_MAX_ENTRIES = 1024
_unknown_kid_cache: dict[str, float] = {}


class _JWKSRefreshState:
    """State shared by callers waiting for one in-flight JWKS refresh.

    The network request deliberately runs outside ``_jwks_lock``.  Callers
    that arrive while a refresh is in progress wait on this state instead of
    issuing duplicate requests to the identity provider.
    """

    def __init__(self) -> None:
        self.event = threading.Event()
        self.data: dict[str, Any] | None = None
        self.error: BaseException | None = None


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
    global _jwks_cache, _jwks_refresh_state
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
    # refresh-state transitions are protected; the network request stays out
    # of the critical section.  A single in-flight state coalesces concurrent
    # refreshes and propagates the leader's result or error to its waiters.
    while True:
        with _jwks_lock:
            cached = _jwks_cache
            now = time.monotonic()
            if cached:
                age = now - cached[0]
                if force and age < _JWKS_FORCE_REFRESH_COOLDOWN:
                    return cached[1]
                if not force and age < settings.jwks_ttl:
                    return cached[1]

            refresh = _jwks_refresh_state
            if refresh is None:
                refresh = _JWKSRefreshState()
                _jwks_refresh_state = refresh
                is_leader = True
            else:
                is_leader = False

        if not is_leader:
            refresh.event.wait()
            if refresh.error is not None:
                raise refresh.error
            if refresh.data is not None:
                return refresh.data
            # Defensive fallback if a future change signals without a result.
            continue

        try:
            response = requests.get(url, timeout=(3, 5), allow_redirects=False)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
                raise RuntimeError("OIDC JWKS 响应格式无效")

            # A concurrent caller cannot start another refresh while this
            # state is active.  Keep the timestamp guard for compatibility
            # with tests or administrative cache publication.
            fetched_at = time.monotonic()
            initial_timestamp = initial_cache[0] if initial_cache else 0.0
            with _jwks_lock:
                current = _jwks_cache
                result = current[1] if current and current[0] > initial_timestamp else data
                if result is data:
                    _jwks_cache = (fetched_at, data)
                refresh.data = result
                refresh.event.set()
                if _jwks_refresh_state is refresh:
                    _jwks_refresh_state = None
                return result
        except BaseException as exc:
            with _jwks_lock:
                refresh.error = exc
                refresh.event.set()
                if _jwks_refresh_state is refresh:
                    _jwks_refresh_state = None
            raise


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
            cutoff = now - _JWKS_FORCE_REFRESH_COOLDOWN
            for cached_kid, seen_at in list(_unknown_kid_cache.items()):
                if seen_at < cutoff:
                    _unknown_kid_cache.pop(cached_kid, None)
            last_unknown = _unknown_kid_cache.get(str(kid))
            if last_unknown is None or now - last_unknown >= _JWKS_FORCE_REFRESH_COOLDOWN:
                if last_unknown is None and len(_unknown_kid_cache) >= _UNKNOWN_KID_CACHE_MAX_ENTRIES:
                    oldest_kid = min(_unknown_kid_cache, key=lambda cached_kid: _unknown_kid_cache[cached_kid])
                    _unknown_kid_cache.pop(oldest_kid, None)
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
    if not settings.app_env_configured or not settings.auth_enabled_configured:
        raise HTTPException(
            status_code=503,
            detail="必须显式配置 TC_STUDIO_APP_ENV 与 TC_STUDIO_AUTH_ENABLED；旧 APP_ENV/AUTH_ENABLED 仍兼容；本地开发请设置 TC_STUDIO_APP_ENV=development、TC_STUDIO_AUTH_ENABLED=false",
        )
    if not settings.enabled:
        if settings.environment not in LOCAL_DEVELOPMENT_ENVIRONMENTS and not settings.allow_insecure_local:
            raise HTTPException(status_code=503, detail="非本地开发环境必须启用认证（TC_STUDIO_AUTH_ENABLED=true），或明确设置 TC_STUDIO_ALLOW_INSECURE_LOCAL_AUTH=true")
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

        async def send_private(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                # The same public BOT URL returns different private state for
                # each identity. Neither browsers nor shared proxies may cache it.
                headers = [(key, value) for key, value in message.get("headers", [])
                           if key.lower() != b"cache-control"]
                headers.append((b"cache-control", b"no-store"))
                headers.append((b"vary", b"Authorization"))
                message = {**message, "headers": headers}
            await send(message)
        try:
            user = authenticate_request(request)
        except HTTPException as exc:
            response = JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers=exc.headers,
            )
            await response(scope, receive, send_private)
            return

        request.state.current_user = user
        scope_token = runtime_auth_scope.set(user.auth_scope)
        token = runtime_user_id.set(user.subject)
        admin_token = runtime_is_admin.set(user.is_admin)
        operator_token = runtime_is_operator.set(user.is_operator)
        try:
            await self.app(scope, receive, send_private)
        finally:
            runtime_is_admin.reset(admin_token)
            runtime_is_operator.reset(operator_token)
            runtime_user_id.reset(token)
            runtime_auth_scope.reset(scope_token)
