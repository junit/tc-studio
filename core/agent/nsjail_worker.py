"""Minimal RPC worker used by the optional Linux nsjail executor.

This module must stay free of application clients and credentials.  It only
loads smolagents' AST evaluator and exposes parent-owned capabilities through
JSON framed RPC proxies.
"""

from __future__ import annotations

import json
import math
import socket
import struct
import sys
from typing import Any

from smolagents import local_python_executor


MAX_IPC_BYTES = 8 * 1024 * 1024
MAX_STATE_BYTES = 512 * 1024


def _wire_value(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> Any:
    if budget is None:
        budget = [MAX_STATE_BYTES]
    if budget[0] <= 0 or depth > 8:
        return "<value truncated>"
    if value is None or isinstance(value, (str, bool, int)):
        size = len(value.encode("utf-8")) if isinstance(value, str) else 8
        budget[0] -= size
        return value if budget[0] >= 0 else "<value truncated>"
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        budget[0] -= 8
        return value if budget[0] >= 0 else "<value truncated>"
    if isinstance(value, (bytes, bytearray)):
        budget[0] -= min(len(value), 256)
        return f"<bytes:{len(value)}>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 1000 or budget[0] <= 0:
                break
            result[str(key)] = _wire_value(item, depth=depth + 1, budget=budget)
        return result
    if isinstance(value, (list, tuple, set)):
        result = []
        for index, item in enumerate(value):
            if index >= 1000 or budget[0] <= 0:
                break
            result.append(_wire_value(item, depth=depth + 1, budget=budget))
        return result
    text = str(value)
    budget[0] -= len(text.encode("utf-8"))
    return text if budget[0] >= 0 else "<value truncated>"


def _state_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    budget = [MAX_STATE_BYTES]
    for key, value in state.items():
        key = str(key)
        if key.startswith("_") or callable(value) or budget[0] <= 0:
            continue
        snapshot[key] = _wire_value(value, budget=budget)
    return snapshot


def _send(sock: socket.socket, message: dict[str, Any]) -> None:
    payload = json.dumps(_wire_value(message, budget=[MAX_IPC_BYTES]), ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(payload) > MAX_IPC_BYTES:
        raise RuntimeError("nsjail worker IPC message exceeds limit")
    sock.sendall(struct.pack("!I", len(payload)) + payload)


def _recv(sock: socket.socket) -> dict[str, Any]:
    header = _read_exact(sock, 4)
    size = struct.unpack("!I", header)[0]
    if size > MAX_IPC_BYTES:
        raise RuntimeError("nsjail worker IPC message exceeds limit")
    message = json.loads(_read_exact(sock, size).decode("utf-8"))
    if not isinstance(message, dict):
        raise RuntimeError("invalid nsjail worker IPC message")
    return message


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise EOFError("nsjail worker RPC peer closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class _RpcClient:
    def __init__(self, sock: socket.socket):
        self.sock = sock

    def call(self, kind: str, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        _send(self.sock, {"kind": kind, "name": name, "args": args, "kwargs": kwargs})
        while True:
            message = _recv(self.sock)
            if message.get("kind") != "response":
                continue
            if message.get("ok"):
                return message.get("value")
            raise RuntimeError(str(message.get("error") or "parent capability call failed"))


class RpcToolProxy:
    def __init__(self, client: _RpcClient, name: str):
        self._client = client
        self.name = name
        self.description = "Parent-owned capability"
        self.inputs: dict[str, Any] = {}
        self.output_type = "object"

    def __getattribute__(self, name: str) -> Any:
        # smolagents only blocks dunder attributes.  Hide all proxy internals
        # as well, otherwise model code could reach the raw RPC socket via
        # ``tool._client`` and forge capability messages.
        if name.startswith("_") and name != "__class__":
            raise AttributeError(name)
        return object.__getattribute__(self, name)

    def forward(self, *args: Any, **kwargs: Any) -> Any:
        client = object.__getattribute__(self, "_client")
        name = object.__getattribute__(self, "name")
        return client.call("tool_call", name, args, kwargs)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.forward(*args, **kwargs)


class RpcFunctionProxy:
    def __init__(self, client: _RpcClient, name: str):
        self._client = client
        self.__name__ = name

    def __getattribute__(self, name: str) -> Any:
        if name.startswith("_") and name != "__class__":
            raise AttributeError(name)
        return object.__getattribute__(self, name)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        client = object.__getattribute__(self, "_client")
        name = object.__getattribute__(self, "__name__")
        return client.call("function_call", name, args, kwargs)


def run_worker(fd: int, authorized_imports: list[str]) -> None:
    sock = socket.socket(fileno=fd)
    client = _RpcClient(sock)
    executor = local_python_executor.LocalPythonExecutor(
        authorized_imports,
        timeout_seconds=None,
    )
    try:
        _send(sock, {"kind": "ready"})
        while True:
            message = _recv(sock)
            kind = message.get("kind")
            if kind == "close":
                return
            if kind == "init":
                tool_names = [str(name) for name in message.get("tool_names", [])]
                function_names = [str(name) for name in message.get("function_names", [])]
                tools = {name: RpcToolProxy(client, name) for name in tool_names}
                executor.additional_functions = {
                    name: RpcFunctionProxy(client, name) for name in function_names
                }
                executor.send_tools(tools)
                state = message.get("state")
                if isinstance(state, dict):
                    executor.state.update(state)
                _send(sock, {"kind": "initialized"})
                continue
            if kind == "variables":
                variables = message.get("variables")
                if isinstance(variables, dict):
                    executor.state.update(variables)
                continue
            if kind != "execute":
                continue
            try:
                result = executor(str(message.get("code") or ""))
                _send(
                    sock,
                    {
                        "kind": "result",
                        "output": result.output,
                        "logs": result.logs,
                        "is_final_answer": result.is_final_answer,
                        "state": _state_snapshot(executor.state),
                    },
                )
            except BaseException as exc:
                _send(
                    sock,
                    {
                        "kind": "error",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "state": _state_snapshot(executor.state),
                    },
                )
    finally:
        sock.close()


def main() -> None:
    if len(sys.argv) != 5 or sys.argv[1] != "--fd" or sys.argv[3] != "--imports-json":
        raise SystemExit("usage: python -m core.agent.nsjail_worker --fd FD --imports-json JSON")
    imports = json.loads(sys.argv[4])
    if not isinstance(imports, list) or not all(isinstance(item, str) for item in imports):
        raise SystemExit("authorized imports must be a JSON string list")
    run_worker(int(sys.argv[2]), imports)


if __name__ == "__main__":
    main()
