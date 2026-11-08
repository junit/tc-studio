from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def test_config_path_can_be_moved_to_shared_runtime_directory(tmp_path):
    target = tmp_path / "config.json"
    project_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["TIM_CONFIG_PATH"] = str(target)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from core.config import CONFIG_PATH; print(CONFIG_PATH)",
        ],
        cwd=str(project_root),
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip() == str(target.resolve())
