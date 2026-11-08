from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def test_bot_history_append_is_serialized_across_processes(tmp_path):
    data_dir = tmp_path / "data"
    setup = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from core.agent.bot_manager import BotManager; "
                "BotManager(data_dir=__import__('sys').argv[1]).create_bot("
                "'history-race', 'History race')"
            ),
            str(data_dir),
        ],
        cwd=str(Path(__file__).parents[1]),
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])},
        capture_output=True,
        text=True,
        check=False,
    )
    assert setup.returncode == 0, setup.stderr

    child_code = """
from core.agent.bot_manager import BotManager
import sys
manager = BotManager(data_dir=sys.argv[1])
for index in range(30):
    manager.append_history("history-race", "user", f"{sys.argv[2]}-{index}")
"""
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])}
    children = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                child_code,
                str(data_dir),
                f"worker-{index}",
            ],
            cwd=str(Path(__file__).parents[1]),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(2)
    ]
    outputs = [child.communicate(timeout=20) for child in children]
    assert all(child.returncode == 0 for child in children), outputs

    verify = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys; "
                "from core.agent.bot_manager import BotManager; "
                "rows=BotManager(data_dir=sys.argv[1]).get_history("
                "'history-race', limit=1000); "
                "print(json.dumps({'count': len(rows), 'ids': [r['id'] for r in rows], "
                "'contents': [r['content'] for r in rows]}))"
            ),
            str(data_dir),
        ],
        cwd=str(Path(__file__).parents[1]),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 0, verify.stderr
    payload = json.loads(verify.stdout.splitlines()[-1])
    assert payload["count"] == 60
    assert len(set(payload["ids"])) == 60
    assert {content.split("-", 1)[0] for content in payload["contents"]} == {
        "worker",
    }
