from __future__ import annotations

import time

import pytest


def test_old_unknown_idempotency_key_cannot_be_reused(tmp_path, monkeypatch):
    import core.task_runner as task_runner_module
    from core.persistence import open_database

    database = tmp_path / "tim.sqlite3"
    monkeypatch.setattr(task_runner_module, "IDEMPOTENCY_DB_PATH", str(database))
    monkeypatch.setattr(task_runner_module, "STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setattr(
        task_runner_module,
        "RESULTS_DIR",
        str(tmp_path / "storage" / "batch_results"),
    )

    runner = task_runner_module.TaskRunner()
    original = runner.create_task(
        "c2c_msg_withdraw",
        [{"id": "unknown-side-effect"}],
        delay_sec=0,
        dry_run=False,
        owner_id="owner",
        allow_high_risk=True,
        idempotency_key="unknown-retention-key",
        request_fingerprint="fingerprint-1",
    )

    old_timestamp = time.time() - task_runner_module.IDEMPOTENCY_RETENTION_SECONDS - 1
    connection = open_database(str(database))
    try:
        connection.execute(
            """
            UPDATE idempotency_keys
            SET state = 'UNKNOWN', created_at = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (old_timestamp, old_timestamp, original.task_id),
        )
        connection.execute(
            """
            UPDATE batch_tasks
            SET status = 'UNKNOWN', created_at = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (old_timestamp, old_timestamp, original.task_id),
        )
        connection.commit()
    finally:
        connection.close()

    recovery_runner = task_runner_module.TaskRunner()
    replay = recovery_runner.create_task(
        "c2c_msg_withdraw",
        [{"id": "unknown-side-effect"}],
        delay_sec=0,
        dry_run=False,
        owner_id="owner",
        allow_high_risk=True,
        idempotency_key="unknown-retention-key",
        request_fingerprint="fingerprint-1",
    )
    assert replay.task_id == original.task_id
    assert replay.idempotent_replay is True
    assert replay.replay_only is True
    assert recovery_runner.start_task(replay.task_id, recover_reserved=True) is False

    with pytest.raises(task_runner_module.IdempotencyConflict):
        recovery_runner.create_task(
            "c2c_msg_withdraw",
            [{"id": "different-payload"}],
            delay_sec=0,
            dry_run=False,
            owner_id="owner",
            allow_high_risk=True,
            idempotency_key="unknown-retention-key",
            request_fingerprint="fingerprint-2",
        )


def test_expired_terminal_idempotency_records_are_gc_eligible(tmp_path, monkeypatch):
    import core.task_runner as task_runner_module
    from core.persistence import open_database

    database = tmp_path / "tim.sqlite3"
    monkeypatch.setattr(task_runner_module, "IDEMPOTENCY_DB_PATH", str(database))
    old_timestamp = time.time() - task_runner_module.IDEMPOTENCY_RETENTION_SECONDS - 1
    connection = open_database(str(database))
    try:
        connection.execute(
            """
            INSERT INTO idempotency_keys (
                issuer, tenant_id, owner_subject, operation_kind,
                idempotency_key, request_hash, task_id, api_id, total,
                delay_sec, dry_run, state, created_at, updated_at
            ) VALUES ('', '', 'owner', 'batch', 'expired-key', 'hash',
                      'expired-task', 'c2c_msg_withdraw', 1, 0, 1,
                      'COMPLETED', ?, ?)
            """,
            (old_timestamp, old_timestamp),
        )
        connection.commit()
    finally:
        connection.close()

    assert task_runner_module.cleanup_expired_idempotency_records() == 1
    connection = open_database(str(database))
    try:
        assert connection.execute(
            "SELECT 1 FROM idempotency_keys WHERE idempotency_key = 'expired-key'"
        ).fetchone() is None
    finally:
        connection.close()
