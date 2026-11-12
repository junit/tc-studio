from pathlib import Path
import socket
import threading

import pytest

from core.agent.file_capabilities import FILE_OPERATION, FileCapabilityBroker, RemoteFile


def test_nsjail_file_proxy_uses_bounded_socket_rpc_without_host_objects(tmp_path):
    from core.agent.nsjail_worker import _RpcClient, RpcFunctionProxy, _recv, _send
    from core.agent.engine import NsJailPythonExecutor, _confined_open

    left, right = socket.socketpair()
    owner = NsJailPythonExecutor.__new__(NsJailPythonExecutor)
    owner.additional_functions = {"open": lambda *args, **kwargs: _confined_open(str(tmp_path), *args, **kwargs)}
    owner._file_broker = FileCapabilityBroker()
    owner._tools = {}
    errors = []

    def serve():
        try:
            for _ in range(6):  # open/write/close/open/read/close
                request = _recv(right)
                try:
                    value = owner._dispatch_capability(request)
                    _send(right, {"kind": "response", "ok": True, "value": value})
                except Exception as exc:
                    _send(right, {"kind": "response", "ok": False, "error": str(exc)})
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        proxy = RpcFunctionProxy(_RpcClient(left), "open")
        with proxy("report.bin", "wb") as stream:
            assert isinstance(stream, RemoteFile)
            assert stream.write(b"\x00\xffbinary") == 8
        with proxy("report.bin", "rb") as stream:
            assert stream.read() == b"\x00\xffbinary"
    finally:
        left.close()
        right.close()
        thread.join(timeout=3)
        owner._file_broker.close_all()
    assert not thread.is_alive()
    assert errors == []


def test_file_proxy_exposes_no_host_attributes_and_closes_handles(tmp_path):
    broker = FileCapabilityBroker()
    raw = open(tmp_path / "report.txt", "w+")
    proxy = RemoteFile(broker.call, broker.register(raw))
    for attr in ("_call", "_handle", "buffer", "raw", "fileno", "__dict__", "__globals__"):
        with pytest.raises(AttributeError):
            getattr(proxy, attr)
    assert proxy.write("中文\nline2\n") == 9
    proxy.seek(0)
    assert proxy.readline() == "中文\n"
    assert proxy.readlines() == ["line2\n"]
    proxy.close()
    assert raw.closed and proxy.closed
    proxy.close()
    with pytest.raises(ValueError, match="句柄"):
        broker.call("forged-other-executor-handle", "read", 1)


def test_file_broker_rejects_raw_operations_and_resource_overflow(tmp_path, monkeypatch):
    import core.agent.file_capabilities as caps

    broker = FileCapabilityBroker()
    raw = open(tmp_path / "report.txt", "w+")
    raw.write("abcdefghij")
    raw.seek(0)
    info = broker.register(raw)
    for operation in ("fileno", "truncate", "__getattribute__", "detach"):
        with pytest.raises(ValueError, match="不允许"):
            broker.call(info["handle"], operation)
    monkeypatch.setattr(caps, "MAX_READ_UNITS", 4)
    with pytest.raises(ValueError, match="分块读取"):
        broker.call(info["handle"], "read")
    broker.call(info["handle"], "seek", 0)
    monkeypatch.setattr(caps, "MAX_TOTAL_READ_UNITS", 3)
    with pytest.raises(ValueError, match="累计"):
        broker.call(info["handle"], "read", 4)
    monkeypatch.setattr(caps, "MAX_OPEN_FILES", 1)
    rejected = open(tmp_path / "rejected.txt", "w")
    with pytest.raises(ValueError, match="打开"):
        broker.register(rejected)
    assert rejected.closed
    monkeypatch.setattr(caps, "MAX_FILE_OPERATIONS", 0)
    with pytest.raises(ValueError, match="10000"):
        broker.call(info["handle"], "flush")
    broker.close_all()
    assert raw.closed


def test_real_executor_closes_unclosed_files_and_releases_quota_cache(tmp_path):
    from core.agent.engine import KillablePythonExecutor, _confined_open, _SANDBOX_QUOTA_TOTALS

    streams = []

    def open_scratch(*args, **kwargs):
        stream = _confined_open(str(tmp_path), *args, **kwargs)
        streams.append(stream)
        return stream

    executor = KillablePythonExecutor([], timeout_seconds=3)
    executor.additional_functions["open"] = open_scratch
    executor.send_tools({})
    try:
        result = executor("f = open('report.txt', 'w')\nf.write('pending content')\nprint(f.tell())")
        assert "15" in result.logs
    finally:
        executor.close()
    assert streams[0].closed
    assert Path(tmp_path / "report.txt").read_text() == "pending content"
    assert str(tmp_path.resolve()) not in _SANDBOX_QUOTA_TOTALS
