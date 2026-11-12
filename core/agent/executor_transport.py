"""Bounded JSON transport for evaluator capabilities; never unpickle model data.

State/observation summaries are deliberately lossy and are prepared separately.
Capability arguments must arrive intact or fail, especially binary file data.
"""

from __future__ import annotations

import base64
import json
import math
from typing import Any

_TAG = "$tc_studio_wire"


def encode_message(message: dict[str, Any], max_bytes: int) -> bytes:
    budget = [max_bytes]

    def encode(value: Any, depth: int = 0) -> Any:
        if depth > 32 or budget[0] <= 0:
            raise ValueError("执行器 IPC 数据超过大小或嵌套深度上限")
        budget[0] -= 8
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("执行器 IPC 不接受非有限数值")
            return value
        if isinstance(value, str):
            budget[0] -= len(value.encode("utf-8"))
            result = value
        elif isinstance(value, (bytes, bytearray)):
            # Bound before allocating the base64 representation.
            budget[0] -= 4 * ((len(value) + 2) // 3)
            if budget[0] < 0:
                raise ValueError("执行器 IPC 二进制数据超过大小上限")
            result = {_TAG: "bytes", "data": base64.b64encode(value).decode("ascii")}
        elif isinstance(value, (list, tuple)):
            result = [encode(item, depth + 1) for item in value]
        elif isinstance(value, dict):
            items = {}
            for key, item in value.items():
                key = str(key)
                budget[0] -= len(key.encode("utf-8"))
                items[key] = encode(item, depth + 1)
            # Escape literal dictionaries containing the protocol tag, so data
            # resembling a binary envelope is never reinterpreted as bytes.
            result = {_TAG: "dict", "items": items} if _TAG in items else items
        else:
            raise ValueError(f"执行器 IPC 不支持参数类型 {type(value).__name__}")
        if budget[0] < 0:
            raise ValueError("执行器 IPC 数据超过大小上限")
        return result

    payload = json.dumps(encode(message), ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(payload) > max_bytes:
        raise ValueError("执行器 IPC 消息超过大小上限")
    return payload


def decode_message(payload: bytes, max_bytes: int) -> dict[str, Any]:
    if len(payload) > max_bytes:
        raise ValueError("执行器 IPC 消息超过大小上限")

    def decode(value: Any, depth: int = 0) -> Any:
        if depth > 32:
            raise ValueError("执行器 IPC 嵌套深度超过上限")
        if isinstance(value, list):
            return [decode(item, depth + 1) for item in value]
        if isinstance(value, dict):
            if _TAG not in value:
                return {key: decode(item, depth + 1) for key, item in value.items()}
            if value[_TAG] == "bytes" and set(value) == {_TAG, "data"}:
                if isinstance(value["data"], str):
                    return base64.b64decode(value["data"], validate=True)
            if value[_TAG] == "dict" and set(value) == {_TAG, "items"}:
                if isinstance(value["items"], dict):
                    return {key: decode(item, depth + 1) for key, item in value["items"].items()}
            raise ValueError("执行器 IPC 二进制信封格式无效")
        return value

    message = decode(json.loads(payload))
    if not isinstance(message, dict):
        raise ValueError("执行器 IPC 消息格式无效")
    return message
