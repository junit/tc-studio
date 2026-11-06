from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import core.task_runner as task_runner_module


def test_cleanup_orphan_task_results_keeps_referenced_and_recent_files(tmp_path, monkeypatch):
    results_dir = tmp_path / "batch_results"
    history_path = tmp_path / "history.json"
    results_dir.mkdir()
    history_path.write_text(json.dumps([{"task_id": "kept"}]), encoding="utf-8")
    monkeypatch.setattr(task_runner_module, "RESULTS_DIR", str(results_dir))
    monkeypatch.setattr(task_runner_module, "HISTORY_PATH", str(history_path))

    kept = results_dir / "task_kept.jsonl"
    orphan = results_dir / "task_orphan.jsonl"
    recent = results_dir / "task_recent.jsonl"
    for path in (kept, orphan, recent):
        path.write_text("{}\n", encoding="utf-8")
    old = time.time() - 31 * 24 * 60 * 60
    os.utime(kept, (old, old))
    os.utime(orphan, (old, old))

    assert task_runner_module.cleanup_orphan_task_results() == 1
    assert kept.exists()
    assert not orphan.exists()
    assert recent.exists()
