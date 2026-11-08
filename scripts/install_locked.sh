#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is required; install uv before running this script" >&2
  exit 1
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "error: Python interpreter not found: $PYTHON_BIN" >&2
  exit 1
fi

"$PYTHON_BIN" - <<'PY'
import sys

minimum = (3, 11)
maximum = (3, 14)
if sys.version_info < minimum or sys.version_info >= maximum:
    raise SystemExit(
        "error: TC Studio requires Python >=3.11,<3.14; "
        f"found {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
print(
    "python:",
    f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
)
PY

cd "$REPO_ROOT"

# --locked refuses to resolve or rewrite the lock file.  --no-dev creates the
# same production environment used by the systemd service.
uv sync --locked --no-dev
uv pip check --python .venv/bin/python
uv run --locked --no-dev python -m compileall -q core apis scripts

echo "locked production environment is ready"
