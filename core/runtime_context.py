from contextvars import ContextVar
from dataclasses import dataclass


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
