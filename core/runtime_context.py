from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from typing import Callable


@dataclass(frozen=True)
class AuthScope:
    """Verified identity scope carried through request and worker boundaries."""

    issuer: str = ""
    tenant_id: str = ""
    subject: str = "local"


runtime_auth_scope: ContextVar[AuthScope] = ContextVar(
    "runtime_auth_scope",
    default=AuthScope(),
)

runtime_user_id: ContextVar[str] = ContextVar("runtime_user_id", default="local")
runtime_is_admin: ContextVar[bool] = ContextVar("runtime_is_admin", default=True)
runtime_is_operator: ContextVar[bool] = ContextVar("runtime_is_operator", default=True)
runtime_capability_cancelled: ContextVar[Callable[[], bool] | None] = ContextVar(
    "runtime_capability_cancelled", default=None,
)


@contextmanager
def capability_cancellation(cancelled: Callable[[], bool]):
    """Compose nested executor cancellation without exposing handles to code."""
    inherited = runtime_capability_cancelled.get()
    token = runtime_capability_cancelled.set(lambda: cancelled() or bool(inherited and inherited()))
    try:
        yield
    finally:
        runtime_capability_cancelled.reset(token)


def runtime_user_partition(user_id: str | None = None) -> str | None:
    """Return the shared filesystem partition rule for public BOT resources."""
    user_id = runtime_user_id.get() if user_id is None else str(user_id)
    scope = runtime_auth_scope.get()
    if user_id == "local" and not scope.issuer and not scope.tenant_id:
        return None
    identity = "\0".join((scope.issuer, scope.tenant_id, user_id))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
