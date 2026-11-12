"""Bounded file operations over RPC; host file objects never enter the worker."""

from __future__ import annotations

import os
import threading
import uuid
from contextlib import suppress
from typing import Any, Callable

FILE_OPERATION = "$tc_studio_file_operation"
MAX_OPEN_FILES = 32
MAX_READ_UNITS = 512 * 1024  # UTF-8 expansion still fits within the 8 MiB frame.
MAX_FILE_OPERATIONS = 10_000
MAX_TOTAL_READ_UNITS = 64 * 1024 * 1024


class FileCapabilityBroker:
    def __init__(self):
        self._files: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._operations = 0
        self._read_units = 0

    def register(self, stream: Any) -> dict[str, Any]:
        with self._lock:
            if len(self._files) >= MAX_OPEN_FILES:
                stream.close()
                raise ValueError("沙箱同时打开的文件超过 32 个，请先关闭文件")
            handle = uuid.uuid4().hex
            self._files[handle] = stream
            return {"handle": handle, "name": os.path.basename(str(stream.name)), "mode": str(stream.mode),
                    "encoding": getattr(stream, "encoding", None)}

    def call(self, handle: str, operation: str, *args: Any) -> Any:
        with self._lock:
            self._operations += 1
            if self._operations > MAX_FILE_OPERATIONS and operation != "close":
                raise ValueError("沙箱文件操作超过 10000 次限制")
            return self._dispatch(handle, operation, *args)

    def _dispatch(self, handle: str, operation: str, *args: Any) -> Any:
        stream = self._files.get(handle)
        if stream is None:
            raise ValueError("沙箱文件句柄不存在或已关闭")
        if operation == "close":
            try:
                return stream.close()
            finally:
                self._files.pop(handle, None)
        if operation in {"read", "readline"}:
            size = args[0] if args else -1
            if not isinstance(size, int) or size < -1 or size > MAX_READ_UNITS:
                raise ValueError("单次文件读取超过 512 Ki 字节/字符限制，请分块读取")
            value = getattr(stream, operation)(MAX_READ_UNITS + 1 if size == -1 else size)
            self._read_units += len(value)
            if self._read_units > MAX_TOTAL_READ_UNITS:
                raise ValueError("本次执行累计文件读取超过 64 Mi 字节/字符限制")
            if len(value) > MAX_READ_UNITS:
                raise ValueError("单次文件读取超过 512 Ki 字节/字符限制，请分块读取")
            return value
        if operation == "write":
            if len(args) != 1 or not isinstance(args[0], (str, bytes, bytearray)):
                raise ValueError("文件写入需要文本或字节")
            return stream.write(args[0])
        if operation == "seek":
            if not 1 <= len(args) <= 2 or not all(isinstance(value, int) for value in args):
                raise ValueError("seek 参数无效")
            return stream.seek(*args)
        if operation in {"tell", "flush"} and not args:
            return getattr(stream, operation)()
        raise ValueError("不允许的沙箱文件操作")

    def close_all(self) -> None:
        with self._lock:
            for stream in self._files.values():
                with suppress(Exception):
                    stream.close()
            self._files.clear()


class RemoteFile:
    """A small Python file interface backed by an opaque per-executor handle."""

    def __init__(self, call: Callable[..., Any], info: dict[str, Any]):
        self._call = call
        self._handle = info["handle"]
        self.name = info["name"]
        self.mode = info["mode"]
        self.encoding = info["encoding"]
        self.closed = False

    def __getattribute__(self, name: str) -> Any:
        if name.startswith("_") and name not in {"__class__", "__enter__", "__exit__", "__iter__", "__next__"}:
            raise AttributeError(name)
        return object.__getattribute__(self, name)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
        return False

    def read(self, size=-1):
        return object.__getattribute__(self, "_call")(
            object.__getattribute__(self, "_handle"), "read", size)

    def readline(self, size=-1):
        return object.__getattribute__(self, "_call")(
            object.__getattribute__(self, "_handle"), "readline", size)

    def readlines(self, hint=-1):
        if not isinstance(hint, int):
            raise ValueError("readlines hint 必须是整数")
        result = []
        size = 0
        while True:
            line = self.readline()
            if not line:
                return result
            result.append(line)
            size += len(line)
            if size > MAX_READ_UNITS:
                raise ValueError("readlines 超过 512 Ki 限制，请逐行读取")
            if hint > 0 and size >= hint:
                return result

    def write(self, value):
        return object.__getattribute__(self, "_call")(
            object.__getattribute__(self, "_handle"), "write", value)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def seek(self, offset, whence=0):
        return object.__getattribute__(self, "_call")(
            object.__getattribute__(self, "_handle"), "seek", offset, whence)

    def tell(self):
        return object.__getattribute__(self, "_call")(
            object.__getattribute__(self, "_handle"), "tell")

    def flush(self):
        return object.__getattribute__(self, "_call")(
            object.__getattribute__(self, "_handle"), "flush")

    def close(self):
        if not self.closed:
            object.__getattribute__(self, "_call")(
                object.__getattribute__(self, "_handle"), "close")
            self.closed = True

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def readable(self):
        return "r" in self.mode or "+" in self.mode

    def writable(self):
        return any(flag in self.mode for flag in "wax+")

    def seekable(self):
        return True
