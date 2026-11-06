"""Regression tests for the model-facing sandbox ``open`` capability."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.agent import engine


def _small_quotas(monkeypatch):
    # Keep the tests cheap while exercising the same checks as the production
    # 100 MiB per-file and 512 MiB per-bot limits.
    monkeypatch.setattr(engine, "MAX_GENERATED_FILE_BYTES", 8)
    monkeypatch.setattr(engine, "MAX_GENERATED_TOTAL_BYTES", 10)


def test_confined_open_preserves_text_binary_read_and_context(tmp_path, monkeypatch):
    _small_quotas(monkeypatch)
    uploads = tmp_path / "uploads"
    uploads.mkdir()

    with engine._confined_open(uploads, "text.txt", "w", encoding="utf-8") as stream:
        assert stream.write("hello") == 5
        assert stream.closed is False
    with engine._confined_open(uploads, "text.txt", "r", encoding="utf-8") as stream:
        assert stream.read() == "hello"

    with engine._confined_open(uploads, "bytes.bin", "wb") as stream:
        assert stream.write(b"\x00\x01") == 2
    with engine._confined_open(uploads, "bytes.bin", "rb") as stream:
        assert stream.read() == b"\x00\x01"


def test_confined_open_rejects_single_file_overflow_without_partial_write(
    tmp_path, monkeypatch
):
    _small_quotas(monkeypatch)
    uploads = tmp_path / "uploads"
    uploads.mkdir()

    with engine._confined_open(uploads, "too-large.bin", "wb") as stream:
        with pytest.raises(ValueError, match="100 MiB"):
            stream.write(b"123456789")

    assert (uploads / "too-large.bin").read_bytes() == b""


def test_confined_open_rejects_bot_total_overflow_and_allows_replacement(
    tmp_path, monkeypatch
):
    _small_quotas(monkeypatch)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "first.bin").write_bytes(b"12345678")

    with engine._confined_open(uploads, "second.bin", "wb") as stream:
        with pytest.raises(ValueError, match="512 MiB"):
            stream.write(b"123")
    assert (uploads / "second.bin").read_bytes() == b""

    # Replacing an existing file accounts for the old target size only once;
    # the same 8-byte total remains valid after an in-place rewrite.
    with engine._confined_open(uploads, "first.bin", "wb") as stream:
        assert stream.write(b"abcdefgh") == 8
    assert (uploads / "first.bin").read_bytes() == b"abcdefgh"


def test_confined_open_guards_append_seek_truncate_and_writelines(
    tmp_path, monkeypatch
):
    _small_quotas(monkeypatch)
    uploads = tmp_path / "uploads"
    uploads.mkdir()

    with engine._confined_open(uploads, "append.txt", "w", encoding="utf-8") as stream:
        stream.write("1234")
    with engine._confined_open(uploads, "append.txt", "a", encoding="utf-8") as stream:
        stream.writelines(["56", "78"])
        with pytest.raises(ValueError, match="100 MiB"):
            stream.write("9")
    assert (uploads / "append.txt").read_text(encoding="utf-8") == "12345678"

    with engine._confined_open(uploads, "sparse.bin", "wb") as stream:
        stream.seek(8)
        with pytest.raises(ValueError, match="100 MiB"):
            stream.write(b"x")
        with pytest.raises(ValueError, match="100 MiB"):
            stream.truncate(9)
    assert (uploads / "sparse.bin").stat().st_size == 0


def test_confined_open_hides_raw_write_escape_hatches(tmp_path, monkeypatch):
    _small_quotas(monkeypatch)
    uploads = tmp_path / "uploads"
    uploads.mkdir()

    with engine._confined_open(uploads, "escape.bin", "wb") as stream:
        for name in ("buffer", "raw", "detach", "fileno", "__dict__"):
            with pytest.raises(AttributeError):
                getattr(stream, name)
