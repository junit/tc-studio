"""TC Studio runtime namespace and legacy TIM compatibility.

``TIM`` is still a valid Tencent Cloud IM service identifier inside the
application. This module only governs names owned by the TC Studio
application itself: environment variables, runtime paths, and deployment
slugs.

The compatibility rule is deliberately one-way:

* ``TC_STUDIO_*`` is the canonical namespace.
* legacy ``TIM_*`` values are read when the canonical value is absent.
* an unprefixed legacy alias is supported only where the old application
  actually used one.
* canonical values win when both namespaces are present.
* legacy use is logged without logging the value, so credentials cannot leak
  through migration diagnostics.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
import os
from pathlib import Path
import threading
from typing import Any


LOGGER = logging.getLogger(__name__)

APP_DISPLAY_NAME = "TC Studio"
APP_SLUG = "tc-studio"
LEGACY_APP_SLUG = "tim"

CANONICAL_ENV_PREFIX = "TC_STUDIO_"
LEGACY_ENV_PREFIX = "TIM_"

CANONICAL_DATABASE_FILENAME = "tc_studio.sqlite3"
LEGACY_DATABASE_FILENAME = "tim.sqlite3"

# The executor safety switch historically used an unprefixed name. Keep it
# readable during migration while making the TC Studio name canonical.
_EXTRA_LEGACY_ENV_ALIASES: dict[str, tuple[str, ...]] = {
    f"{CANONICAL_ENV_PREFIX}ALLOW_UNCONFINED_CODE_EXECUTOR": (
        "ALLOW_UNCONFINED_CODE_EXECUTOR",
    ),
}

_warning_lock = threading.Lock()
_emitted_warnings: set[tuple[str, str]] = set()


def canonical_env_name(name: str) -> str:
    """Return the canonical ``TC_STUDIO_*`` name for a setting."""

    value = str(name).strip()
    if value.startswith(CANONICAL_ENV_PREFIX):
        return value
    if value.startswith(LEGACY_ENV_PREFIX):
        return CANONICAL_ENV_PREFIX + value[len(LEGACY_ENV_PREFIX) :]
    return CANONICAL_ENV_PREFIX + value


def legacy_env_names(name: str) -> tuple[str, ...]:
    """Return legacy aliases in precedence order for a canonical setting."""

    canonical = canonical_env_name(name)
    suffix = canonical[len(CANONICAL_ENV_PREFIX) :]
    candidates = [f"{LEGACY_ENV_PREFIX}{suffix}"]
    candidates.extend(_EXTRA_LEGACY_ENV_ALIASES.get(canonical, ()))
    result: list[str] = []
    for candidate in candidates:
        if candidate != canonical and candidate not in result:
            result.append(candidate)
    return tuple(result)


def _non_blank(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _warn_once(key: str, message: str) -> None:
    marker = (key, message)
    with _warning_lock:
        if marker in _emitted_warnings:
            return
        _emitted_warnings.add(marker)
    LOGGER.warning(message)


def get_env(
    name: str,
    *,
    default: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Read a TC Studio setting with a safe legacy fallback.

    ``name`` may be supplied as ``DATABASE_URL``, ``TC_STUDIO_DATABASE_URL``
    or ``TIM_DATABASE_URL``. The returned value is always selected using the
    canonical name first. Warning messages contain names only and never
    values, which is important for secret-bearing aliases.
    """

    environment = os.environ if environ is None else environ
    canonical = canonical_env_name(name)
    canonical_value = _non_blank(environment.get(canonical))
    legacy_values = [
        (alias, _non_blank(environment.get(alias)))
        for alias in legacy_env_names(canonical)
    ]
    legacy_values = [
        (alias, value) for alias, value in legacy_values if value is not None
    ]

    if canonical_value is not None:
        if any(value != canonical_value for _, value in legacy_values):
            _warn_once(
                f"conflict:{canonical}",
                f"同时设置了 {canonical} 和旧环境变量；以 {canonical} 为准，"
                "旧变量仅用于兼容读取",
            )
        elif legacy_values:
            _warn_once(
                f"legacy-present:{canonical}",
                f"检测到旧环境变量 {legacy_values[0][0]}；请迁移到 {canonical}",
            )
        return canonical_value

    if legacy_values:
        alias, value = legacy_values[0]
        _warn_once(
            f"legacy-used:{canonical}",
            f"使用旧环境变量 {alias}；请迁移到 {canonical}",
        )
        if any(other_value != value for _, other_value in legacy_values[1:]):
            _warn_once(
                f"legacy-conflict:{canonical}",
                f"同时设置了多个旧环境变量；以 {alias} 为准",
            )
        return value

    return default


def select_canonical_or_legacy_path(
    canonical_path: str | os.PathLike[str],
    legacy_path: str | os.PathLike[str],
    *,
    label: str = "运行文件",
) -> str:
    """Choose a canonical path while keeping an existing legacy path usable.

    This function never moves or deletes either file. A separate, explicit
    migration command can perform the copy after a backup and verification.
    """

    canonical = Path(canonical_path).expanduser()
    legacy = Path(legacy_path).expanduser()
    canonical_exists = canonical.exists()
    legacy_exists = legacy.exists()

    if canonical_exists:
        if legacy_exists:
            _warn_once(
                f"path-both:{canonical.resolve()}",
                f"{label}同时存在新旧路径；以新路径 {canonical} 为准，"
                f"旧路径 {legacy} 不会自动合并",
            )
        return str(canonical.resolve())

    if legacy_exists:
        _warn_once(
            f"path-legacy:{legacy.resolve()}",
            f"使用旧{label}路径 {legacy}；请迁移到 {canonical}",
        )
        return str(legacy.resolve())

    return str(canonical.resolve())


def reset_warning_state_for_tests() -> None:
    """Reset one-time warnings for isolated tests."""

    with _warning_lock:
        _emitted_warnings.clear()
