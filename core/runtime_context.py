from contextvars import ContextVar

runtime_user_id: ContextVar[str] = ContextVar("runtime_user_id", default="local")
runtime_is_admin: ContextVar[bool] = ContextVar("runtime_is_admin", default=True)
runtime_is_operator: ContextVar[bool] = ContextVar("runtime_is_operator", default=True)
