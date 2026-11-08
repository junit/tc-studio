from __future__ import annotations

import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


def test_canonical_namespace_wins_without_logging_secret_values(caplog):
    from core.namespace import get_env, reset_warning_state_for_tests

    reset_warning_state_for_tests()
    caplog.set_level(logging.WARNING, logger="core.namespace")
    environment = {
        "TC_STUDIO_SECRET_KEY": "namespace-test-canonical",
        "TIM_SECRET_KEY": "namespace-test-legacy",
    }

    assert get_env("SECRET_KEY", environ=environment) == "namespace-test-canonical"
    assert "TC_STUDIO_SECRET_KEY" in caplog.text
    assert "namespace-test-canonical" not in caplog.text
    assert "namespace-test-legacy" not in caplog.text


def test_legacy_namespace_is_read_when_canonical_value_is_absent(caplog):
    from core.namespace import get_env, reset_warning_state_for_tests

    reset_warning_state_for_tests()
    caplog.set_level(logging.WARNING, logger="core.namespace")
    environment = {"TIM_DATA_DIR": "/var/lib/tim/data"}

    assert get_env("DATA_DIR", environ=environment) == "/var/lib/tim/data"
    assert "TIM_DATA_DIR" in caplog.text
    assert "TC_STUDIO_DATA_DIR" in caplog.text


def test_prefixed_legacy_namespace_wins_over_historical_unprefixed_alias():
    from core.namespace import get_env, reset_warning_state_for_tests

    reset_warning_state_for_tests()
    environment = {
        "TIM_ALLOW_UNCONFINED_CODE_EXECUTOR": "tim-value",
        "ALLOW_UNCONFINED_CODE_EXECUTOR": "unprefixed-value",
    }

    assert (
        get_env("ALLOW_UNCONFINED_CODE_EXECUTOR", environ=environment)
        == "tim-value"
    )


def test_existing_legacy_database_is_used_until_explicit_migration(tmp_path):
    from core.namespace import select_canonical_or_legacy_path

    canonical = tmp_path / "tc_studio.sqlite3"
    legacy = tmp_path / "tim.sqlite3"
    legacy.touch()

    selected = select_canonical_or_legacy_path(canonical, legacy, label="SQLite 主库")

    assert selected == str(legacy.resolve())
    assert not canonical.exists()
    assert legacy.exists()


def test_canonical_database_wins_when_both_paths_exist(tmp_path):
    from core.namespace import select_canonical_or_legacy_path

    canonical = tmp_path / "tc_studio.sqlite3"
    legacy = tmp_path / "tim.sqlite3"
    canonical.touch()
    legacy.touch()

    selected = select_canonical_or_legacy_path(canonical, legacy, label="SQLite 主库")

    assert selected == str(canonical.resolve())


def test_explicit_database_migration_copies_and_preserves_legacy_source(tmp_path):
    from scripts.migrate_tim_namespace import migrate_database

    source = tmp_path / "tim.sqlite3"
    target = tmp_path / "tc_studio.sqlite3"
    connection = sqlite3.connect(source)
    try:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker(value) VALUES ('legacy-row')")
        connection.commit()
    finally:
        connection.close()

    migrated = migrate_database(source, target)

    assert migrated == target.resolve()
    assert source.exists()
    copied = sqlite3.connect(target)
    try:
        assert copied.execute("SELECT value FROM marker").fetchone()[0] == "legacy-row"
        assert copied.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        copied.close()


def test_database_migration_refuses_existing_target_without_force(tmp_path):
    from scripts.migrate_tim_namespace import migrate_database

    source = tmp_path / "tim.sqlite3"
    target = tmp_path / "tc_studio.sqlite3"
    source_connection = sqlite3.connect(source)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        source_connection.execute("INSERT INTO marker(value) VALUES ('source')")
        source_connection.commit()
        target_connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        target_connection.execute("INSERT INTO marker(value) VALUES ('existing')")
        target_connection.commit()
    finally:
        source_connection.close()
        target_connection.close()

    with pytest.raises(FileExistsError, match="--force"):
        migrate_database(source, target)

    connection = sqlite3.connect(target)
    try:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "existing"
    finally:
        connection.close()


def test_database_migration_cleans_temporary_file_after_integrity_failure(
    tmp_path, monkeypatch
):
    import scripts.migrate_tim_namespace as migration_module

    source = tmp_path / "tim.sqlite3"
    target = tmp_path / "tc_studio.sqlite3"
    connection = sqlite3.connect(source)
    try:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.commit()
    finally:
        connection.close()

    original_integrity_check = migration_module.integrity_check

    def fail_target_integrity(path: Path) -> str:
        if path == source.resolve():
            return original_integrity_check(path)
        return "not ok"

    monkeypatch.setattr(migration_module, "integrity_check", fail_target_integrity)
    with pytest.raises(RuntimeError, match="完整性校验失败"):
        migration_module.migrate_database(source, target)

    assert not target.exists()
    assert not list(tmp_path.glob(".tc_studio.sqlite3.*.migration.tmp"))


def test_database_env_precedence_is_canonical_then_legacy(monkeypatch, tmp_path):
    from core.persistence import database_path

    canonical = tmp_path / "canonical.sqlite3"
    legacy = tmp_path / "legacy.sqlite3"
    monkeypatch.setenv("TC_STUDIO_DATABASE_URL", f"sqlite:///{canonical}")
    monkeypatch.setenv("TIM_DATABASE_URL", f"sqlite:///{legacy}")
    assert database_path() == str(canonical.resolve())

    monkeypatch.delenv("TC_STUDIO_DATABASE_URL")
    assert database_path() == str(legacy.resolve())


def test_canonical_executor_environment_controls_mode_over_legacy(monkeypatch):
    import core.agent.engine as engine_module

    monkeypatch.setattr(engine_module.sys, "platform", "linux")
    monkeypatch.setattr(engine_module.os, "name", "posix")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("TC_STUDIO_CODE_EXECUTOR_MODE", "spawn")
    monkeypatch.setenv("TIM_CODE_EXECUTOR_MODE", "fork")
    monkeypatch.setenv("TC_STUDIO_ALLOW_UNCONFINED_CODE_EXECUTOR", "true")
    monkeypatch.delenv("ALLOW_UNCONFINED_CODE_EXECUTOR", raising=False)

    executor = engine_module._create_code_executor([])
    try:
        assert isinstance(executor, engine_module.KillablePythonExecutor)
        assert executor.force_spawn is True
    finally:
        executor.close()


def test_config_path_supports_new_and_legacy_environment_names(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    canonical = tmp_path / "tc-studio-config.json"
    legacy = tmp_path / "tim-config.json"

    environment = dict(os.environ)
    environment["TC_STUDIO_CONFIG_PATH"] = str(canonical)
    environment["TIM_CONFIG_PATH"] = str(legacy)
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

    assert completed.stdout.strip() == str(canonical.resolve())
