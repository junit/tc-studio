from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def test_sqlite_multiprocess_acceptance_probe(tmp_path):
    script = Path(__file__).parents[1] / "scripts" / "acceptance_sqlite_multiprocess.py"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(script.parents[1])
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--work-dir",
            str(tmp_path / "probe"),
            "--timeout",
            "20",
        ],
        cwd=str(script.parents[1]),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout[-4000:]}\n"
        f"stderr={result.stderr[-4000:]}"
    )
    payload = json.loads(result.stdout.splitlines()[-1])
    assert all(payload["checks"].values())
    assert payload["controlled_worker_events"]


def test_sqlite_hard_fault_acceptance_probe(tmp_path):
    script = Path(__file__).parents[1] / "scripts" / "acceptance_sqlite_multiprocess.py"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(script.parents[1])
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--work-dir",
            str(tmp_path / "hard-fault-probe"),
            "--timeout",
            "20",
            "--hard-kill",
            "--file-faults",
            "--hard-kill-lease-seconds",
            "2",
        ],
        cwd=str(script.parents[1]),
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout[-4000:]}\n"
        f"stderr={result.stderr[-4000:]}"
    )
    payload = json.loads(result.stdout.splitlines()[-1])
    assert all(payload["checks"].values())
    assert payload["file_faults"]["http_export_checks"]["truncated_export_409"]
    assert payload["file_faults"]["http_export_checks"]["missing_export_409"]


def test_http_sqlite_multiprocess_acceptance_probe(tmp_path):
    script = Path(__file__).parents[1] / "scripts" / "acceptance_http_multiprocess.py"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(script.parents[1])
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--work-dir",
            str(tmp_path / "http-probe"),
            "--timeout",
            "30",
        ],
        cwd=str(script.parents[1]),
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout[-4000:]}\n"
        f"stderr={result.stderr[-4000:]}"
    )
    payload = json.loads(result.stdout.splitlines()[-1])
    assert all(payload["checks"].values())
    assert {item["idempotent_replay"] for item in payload["submissions"]} == {
        False,
        True,
    }
    assert {item["status"] for item in payload["observations"]} == {"COMPLETED"}
