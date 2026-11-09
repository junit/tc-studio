"""Shared paths for runtime caches and package-bundled fallbacks.

The application package is read-only in the production systemd unit.  Files
that can be refreshed at runtime therefore belong below ``TC_STUDIO_STORAGE_DIR``
(or its legacy ``TIM_STORAGE_DIR`` alias), while JSON files shipped in the
package remain read-only fallbacks.
"""

from __future__ import annotations

import os
from pathlib import Path
import re

from .namespace import get_env


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_CORE_DIR = PROJECT_ROOT / "core"
PACKAGE_STORAGE_DIR = PROJECT_ROOT / "storage"


def _absolute_path(value: str | os.PathLike[str], fallback: Path) -> Path:
    raw = str(value).strip() if value is not None else ""
    candidate = Path(raw).expanduser() if raw else fallback
    return Path(os.path.abspath(candidate))


def runtime_storage_dir() -> Path:
    """Return the configured writable runtime storage directory."""

    configured = get_env("STORAGE_DIR", default=str(PACKAGE_STORAGE_DIR))
    return _absolute_path(configured or PACKAGE_STORAGE_DIR, PACKAGE_STORAGE_DIR)


def runtime_docs_dir() -> Path:
    return runtime_storage_dir() / "docs"


def package_docs_dir() -> Path:
    return PACKAGE_STORAGE_DIR / "docs"


def runtime_catalog_dir() -> Path:
    return runtime_storage_dir() / "catalogs"


def _safe_service_name(service: str | None) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]", "_", str(service or "live").lower())
    return value or "live"


def catalog_filename(service: str) -> str:
    """Map the application service id to its catalog filename."""

    service_name = _safe_service_name(service)
    if service_name == "im":
        service_name = "tim"
    return f"{service_name}_full_catalog.json"


def runtime_catalog_path(service: str) -> Path:
    return runtime_catalog_dir() / catalog_filename(service)


def package_catalog_path(service: str) -> Path:
    return PACKAGE_CORE_DIR / catalog_filename(service)


def catalog_read_path(service: str) -> Path:
    """Choose a runtime catalog override, then the package catalog fallback."""

    runtime_path = runtime_catalog_path(service)
    if runtime_path.exists():
        return runtime_path
    return package_catalog_path(service)


def runtime_specs_path(service: str = "live") -> Path:
    return runtime_storage_dir() / f"{_safe_service_name(service)}_common_specs.json"


def package_specs_path(service: str = "live") -> Path:
    return PACKAGE_CORE_DIR / f"{_safe_service_name(service)}_common_specs.json"


def specs_read_path(service: str = "live") -> Path:
    runtime_path = runtime_specs_path(service)
    if runtime_path.exists():
        return runtime_path
    return package_specs_path(service)


def runtime_data_types_path(service: str = "live") -> Path:
    return runtime_storage_dir() / f"{_safe_service_name(service)}_data_types.json"


def package_data_types_path(service: str = "live") -> Path:
    return PACKAGE_CORE_DIR / f"{_safe_service_name(service)}_data_types.json"


def data_types_read_path(service: str = "live") -> Path:
    runtime_path = runtime_data_types_path(service)
    if runtime_path.exists():
        return runtime_path
    return package_data_types_path(service)


def runtime_official_tree_path() -> Path:
    return runtime_storage_dir() / "official_live_tree.json"


def official_tree_read_path() -> Path:
    runtime_path = runtime_official_tree_path()
    if runtime_path.exists():
        return runtime_path
    return PACKAGE_STORAGE_DIR / "official_live_tree.json"


def official_tree_read_paths() -> tuple[Path, ...]:
    runtime_path = runtime_official_tree_path()
    package_path = PACKAGE_STORAGE_DIR / "official_live_tree.json"
    if runtime_path == package_path:
        return (runtime_path,)
    return runtime_path, package_path
