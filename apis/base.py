"""
腾讯云 IM API 基类与字段定义
"""

from __future__ import annotations

import json
import math
from contextlib import suppress
from typing import Any


def safe_int(val: Any, default: int = 0) -> int:
    """安全转换为整型；非法、非有限或非整数输入返回 default。"""
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val) if math.isfinite(val) and val.is_integer() else default
    if isinstance(val, str):
        value = val.strip()
        if not value:
            return default
        signless = value[1:] if value[:1] in ("+", "-") else value
        if signless.isdigit():
            try:
                return int(value)
            except ValueError:
                return default
    return default


def get_input_value(input_data: dict[str, Any], canonical: str, aliases: list[str] | None = None) -> Any:
    """读取字段时保留 canonical 的显式空值，并按声明顺序尝试 aliases。"""
    for key in [canonical, *(aliases or [])]:
        if key in input_data and input_data[key] is not None:
            return input_data[key]
    return None


class Field:
    def __init__(
        self,
        name: str,
        label: str,
        type: str = "string",  # string, number, boolean, json, list
        required: bool = True,
        default: Any = None,
        placeholder: str = "",
        help_text: str = "",
        aliases: list[str] | None = None,  # 用于 Excel/CSV 自动列匹配
        struct_name: str | None = None,
        sub_fields: list[dict] | None = None,
        item_schema: dict | None = None
    ):
        self.name = name
        self.label = label
        self.type = type
        self.required = required
        self.default = default
        self.placeholder = placeholder
        self.help_text = help_text
        self.aliases = aliases or []
        self.struct_name = struct_name
        self.sub_fields = sub_fields
        self.item_schema = item_schema

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "label": self.label,
            "type": self.type,
            "required": self.required,
            "default": self.default,
            "placeholder": self.placeholder,
            "help_text": self.help_text,
            "aliases": self.aliases
        }
        if self.struct_name:
            d["struct_name"] = self.struct_name
        if self.sub_fields:
            d["sub_fields"] = self.sub_fields
        if self.item_schema:
            d["item_schema"] = self.item_schema
        return d


class BaseTimApi:
    api_id: str = ""
    name: str = ""
    service_type: str = "im"  # "im" 或 "live" 等 API 3.0 云服务标识
    category: str = "通用"
    subcategory: str = ""
    path: list[str] = []
    doc_id: int = 0
    service_name: str = ""
    command: str = ""
    action: str = ""
    version: str = "2018-08-01"
    endpoint: str = ""
    doc_url: str = ""
    description: str = ""
    fields: list[Field] = []
    sample_payload: dict[str, Any] = {}

    def build_payload(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """
        根据前端表单或批量行字典转换组装最终的 API 请求包体
        子类可按需覆盖此方法
        """
        payload = {}
        for f in self.fields:
            val = input_data.get(f.name)
            if val is None:
                # 检查别名
                for alias in f.aliases:
                    if alias in input_data and input_data[alias] is not None:
                        val = input_data[alias]
                        break

            if val is None:
                # 忽略大小写及下划线模糊匹配 (如 to_account -> To_Account)
                f_norm = f.name.lower().replace("_", "")
                for k, v in input_data.items():
                    if k.lower().replace("_", "") == f_norm and v is not None:
                        val = v
                        break

            if val is not None:
                f_type = (f.type or "").lower()
                if f_type in ("number", "int", "integer", "float", "double"):
                    if isinstance(val, str) and val.strip():
                        with suppress(ValueError, TypeError):
                            val = int(val) if val.strip().lstrip("-").isdigit() else float(val)
                    if isinstance(val, float) and not math.isfinite(val):
                        raise ValueError(f"字段 {f.name} 必须是有限数值")
                elif f_type in ("boolean", "bool"):
                    if isinstance(val, str):
                        val = val.lower() in ("true", "1", "yes")
                    else:
                        val = bool(val)
                elif f_type in ("list", "array"):
                    if isinstance(val, str):
                        val_str = val.strip()
                        if val_str.startswith("[") and val_str.endswith("]"):
                            try:
                                val = json.loads(val_str)
                            except Exception:
                                val = [x.strip() for x in val_str[1:-1].split(",") if x.strip()]
                        elif val_str:
                            val = [x.strip() for x in val_str.split(",") if x.strip()]
                        else:
                            val = []
                elif f_type in ("json", "object", "dict") and isinstance(val, str):
                    try:
                        val = json.loads(val)
                    except (TypeError, json.JSONDecodeError):
                        val = val
                payload[f.name] = val
            elif f.default is not None:
                payload[f.name] = f.default

        # 如果 input_data 本身已经包含 sample_payload 结构，直接合并未匹配字段
        for k, v in input_data.items():
            if k not in payload and not k.startswith("_"):
                payload[k] = v
        return payload

    def to_metadata(self) -> dict:
        service_type = getattr(self, "service_type", "im") or "im"
        if service_type == "im":
            full_cmd = (
                f"v4/{self.service_name}/{self.command}"
                if self.service_name and not self.command.startswith("v4/")
                else self.command
            )
        else:
            full_cmd = self.command

        return {
            "api_id": self.api_id,
            "service_type": service_type,
            "name": self.name,
            "category": self.category,
            "subcategory": self.subcategory or "常规",
            "path": getattr(self, "path", []) or ([self.category, self.subcategory] if self.subcategory else [self.category]),
            "doc_id": getattr(self, "doc_id", 0),
            "service_name": self.service_name,
            "command": full_cmd,
            "raw_command": self.command,
            "action": getattr(self, "action", "") or self.command,
            "version": getattr(self, "version", "2018-08-01"),
            "endpoint": getattr(self, "endpoint", "") or (f"{self.service_name or service_type}.tencentcloudapi.com" if service_type != "im" else ""),
            "doc_url": self.doc_url,
            "description": self.description,
            "fields": [f.to_dict() for f in self.fields],
            "sample_payload": self.sample_payload
        }
