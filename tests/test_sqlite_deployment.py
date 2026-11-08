from __future__ import annotations

from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_sqlite_deployment


def test_mount_parser_reads_linux_mount_output(monkeypatch):
    def fake_run(command, **_kwargs):
        if command == ["mount", "-p"]:
            return subprocess.CompletedProcess(command, 1, "", "invalid option")
        return subprocess.CompletedProcess(
            command,
            0,
            "overlay on / type overlay (rw,relatime)\n"
            "tmpfs on /tmp type tmpfs (rw,nosuid)\n",
            "",
        )

    monkeypatch.setattr(check_sqlite_deployment.subprocess, "run", fake_run)

    assert check_sqlite_deployment._mounts() == [
        (Path("/"), "overlay"),
        (Path("/tmp"), "tmpfs"),
    ]


def test_mount_parser_reads_macos_mount_output(monkeypatch):
    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            "/dev/disk3s1 on / (apfs, sealed, local)\n",
            "",
        )

    monkeypatch.setattr(check_sqlite_deployment.subprocess, "run", fake_run)

    assert check_sqlite_deployment._mounts() == [(Path("/"), "apfs")]


def test_filesystem_type_prefers_findmnt(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "ext4\n", "")

    monkeypatch.setattr(check_sqlite_deployment.subprocess, "run", fake_run)

    assert check_sqlite_deployment.filesystem_type(tmp_path) == "ext4"
    assert calls == [["findmnt", "-T", str(tmp_path.resolve()), "-n", "-o", "FSTYPE"]]


def test_check_fails_closed_when_filesystem_type_is_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(check_sqlite_deployment, "filesystem_type", lambda _path: None)

    result = check_sqlite_deployment.check(
        str(tmp_path / "tim.sqlite3"),
        tmp_path / "blobs",
        results_root=tmp_path / "results",
        backup_root=tmp_path / "backups",
        data_root=tmp_path / "data",
        storage_root=tmp_path / "storage",
    )

    assert result["passed"] is False
    assert result["filesystem_types_known"] is False
    assert any("无法识别" in error for error in result["errors"])
