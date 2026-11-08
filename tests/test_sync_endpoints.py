"""Regression tests for non-blocking official catalog synchronization."""

from __future__ import annotations

import threading
import time
import os
import sys

import pytest
from fastapi import HTTPException

# Keep this test runnable both from the repository root and from ``tests/``.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app as app_module
from core.auth import CurrentUser
from core.sync_task_manager import SyncTaskManager


OPERATOR = CurrentUser("operator-1", frozenset({"operator"}))


def _wait_for_status(manager: SyncTaskManager, kind: str, task_id: str, expected: str, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = manager.get_status(kind, task_id)
        if status["status"] == expected:
            return status
        time.sleep(0.01)
    return manager.get_status(kind, task_id)


@pytest.fixture
def isolated_sync_manager(monkeypatch):
    manager = SyncTaskManager()
    monkeypatch.setattr(app_module, "sync_task_manager", manager)
    return manager


def test_tree_sync_returns_before_blocking_operation_finishes(monkeypatch, isolated_sync_manager):
    started = threading.Event()
    release = threading.Event()

    def blocking_sync():
        started.set()
        assert release.wait(2.0)
        return {"total_apis": 3}

    monkeypatch.setattr(app_module, "sync_catalog_from_live_tree", blocking_sync)
    began = time.monotonic()
    response = app_module.sync_official_tree(None, OPERATOR)
    elapsed = time.monotonic() - began

    assert elapsed < 0.5
    assert response["status"] == "success"
    assert response["data"]["status"] == "started"
    task_id = response["task_id"]
    assert started.wait(1.0)

    duplicate = app_module.sync_official_tree(None, OPERATOR)
    assert duplicate["task_id"] == task_id
    assert duplicate["data"]["status"] == "already_running"

    release.set()
    final = _wait_for_status(isolated_sync_manager, "docs_tree", task_id, "COMPLETED")
    assert final["result"] == {"total_apis": 3}


def test_live_catalog_sync_is_single_flight_and_publishes_failure(monkeypatch, isolated_sync_manager):
    release = threading.Event()

    def blocking_catalog_sync():
        assert release.wait(2.0)
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr(app_module, "_run_live_catalog_sync", blocking_catalog_sync)
    response = app_module.sync_official_live_catalog("retry-key", OPERATOR)
    task_id = response["task_id"]
    replay = app_module.sync_official_live_catalog("retry-key", OPERATOR)
    assert replay["task_id"] == task_id
    assert replay["data"]["status"] == "idempotent_replay"

    release.set()
    final = _wait_for_status(isolated_sync_manager, "live_catalog", task_id, "FAILED")
    assert final["error"] == "upstream unavailable"


def test_unknown_sync_task_id_is_reported_as_not_found(isolated_sync_manager):
    with pytest.raises(HTTPException) as exc_info:
        app_module.get_official_tree_sync_status("does-not-exist", OPERATOR)
    assert exc_info.value.status_code == 404


def test_sync_task_status_is_owner_scoped(isolated_sync_manager):
    task = isolated_sync_manager.start("docs_tree", lambda: {"ok": True}, owner_id="alice")
    task_id = task["task_id"]
    with pytest.raises(HTTPException) as exc_info:
        app_module.get_official_tree_sync_status(task_id, CurrentUser("bob", frozenset({"operator"})))
    assert exc_info.value.status_code == 403


def test_sync_task_scope_isolated_by_issuer_and_tenant(isolated_sync_manager):
    task = isolated_sync_manager.start(
        "docs_tree",
        lambda: {"ok": True},
        owner_id="same-subject",
        issuer="https://issuer.example",
        tenant_id="tenant-a",
    )
    with pytest.raises(PermissionError):
        isolated_sync_manager.get_status(
            "docs_tree",
            task["task_id"],
            owner_id="same-subject",
            issuer="https://issuer.example",
            tenant_id="tenant-b",
        )
