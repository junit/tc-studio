# -*- coding: utf-8 -*-
"""
API 注册中心与动态发现：整合官方 166 个 REST API 完整树型目录与自定义处理器
"""

import importlib
import json
import os
import pkgutil
import threading
from typing import Dict, List, Optional, Type

from apis.base import BaseTimApi, Field


class DynamicTimApi(BaseTimApi):
    def __init__(self, data: dict):
        self.api_id = data.get("api_id", "")
        self.service_type = data.get("service_type", "im")
        self.doc_id = data.get("doc_id", 0)
        self.name = data.get("name", "")
        self.category = data.get("category", "")
        self.subcategory = data.get("subcategory", "")
        self.path = data.get("path", [])
        self.service_name = data.get("service_name", "")
        self.command = data.get("command", "")
        self.full_command = data.get("full_command", "")
        self.action = data.get("action", data.get("command", ""))
        self.version = data.get("version", "2018-08-01")
        self.endpoint = data.get("endpoint", "")
        self.doc_url = data.get("doc_url", "")
        self.description = data.get("description", "")
        self.sample_payload = data.get("sample_payload", {})
        self.fields = [
            Field(
                name=f.get("name"),
                label=f.get("label", f.get("name")),
                type=f.get("type", "string"),
                required=f.get("required", True),
                default=f.get("default"),
                placeholder=f.get("placeholder", ""),
                help_text=f.get("help_text", ""),
                aliases=f.get("aliases", []),
                struct_name=f.get("struct_name"),
                sub_fields=f.get("sub_fields"),
                item_schema=f.get("item_schema")
            )
            for f in data.get("fields", [])
        ]


class ApiRegistry:
    def __init__(self):
        self._apis: Dict[str, BaseTimApi] = {}
        self._ordered_apis: List[BaseTimApi] = []
        self._discovered = False
        self._lock = threading.RLock()

    def discover(self):
        with self._lock:
            if self._discovered:
                return
            self._discover_unlocked()

    def _discover_unlocked(self):
        # 1. 发现已有的自定义 python 类
        apis_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "apis"))
        custom_apis_by_id: Dict[str, BaseTimApi] = {}
        custom_apis_by_name: Dict[str, BaseTimApi] = {}
        custom_apis_by_full_cmd: Dict[str, BaseTimApi] = {}

        for _, module_name, is_pkg in pkgutil.iter_modules([apis_dir]):
            if module_name == "base" or is_pkg:
                continue
            full_module_name = f"apis.{module_name}"
            try:
                mod = importlib.import_module(full_module_name)
                for attr_name in dir(mod):
                    attr = getattr(mod, attr_name)
                    if isinstance(attr, type) and issubclass(attr, BaseTimApi) and attr is not BaseTimApi:
                        instance = attr()
                        if instance.api_id:
                            custom_apis_by_id[instance.api_id] = instance
                            custom_apis_by_name[instance.name] = instance
                            if instance.service_name and instance.command:
                                custom_apis_by_full_cmd[f"v4/{instance.service_name}/{instance.command}"] = instance
            except Exception as e:
                print(f"[WARN] Failed to load module {full_module_name}: {e}")

        # 2. 动态扫描并读取所有云产品 API 目录 (支持 IM 166 个 API、云直播 CSS 261 项 API 3.0 及未来任意 *_full_catalog.json 插件)
        core_dir = os.path.dirname(__file__)
        catalog_files = []
        if os.path.exists(os.path.join(core_dir, "tim_full_catalog.json")):
            catalog_files.append(("im", os.path.join(core_dir, "tim_full_catalog.json")))
        if os.path.exists(os.path.join(core_dir, "live_full_catalog.json")):
            catalog_files.append(("live", os.path.join(core_dir, "live_full_catalog.json")))

        for fn in sorted(os.listdir(core_dir)):
            if fn.endswith("_full_catalog.json") and fn not in ("tim_full_catalog.json", "live_full_catalog.json"):
                service_code = fn.replace("_full_catalog.json", "")
                catalog_files.append((service_code, os.path.join(core_dir, fn)))

        ordered_list: List[BaseTimApi] = []
        used_custom_api_ids = set()

        for default_service_type, catalog_path in catalog_files:
            if not os.path.exists(catalog_path):
                continue
            with open(catalog_path, "r", encoding="utf-8") as f:
                catalog_data = json.load(f)

            for item in catalog_data:
                api_id = item.get("api_id")
                name = item.get("name")
                full_cmd = item.get("full_command") or item.get("command")
                doc_id = item.get("doc_id")

                # 确保标注 service_type
                if "service_type" not in item:
                    item["service_type"] = default_service_type

                # 兼容清洗：若 name 包含冗余的 (Action) 后缀，清洗为纯净名称
                raw_name = str(item.get("name") or "").strip()
                action_str = str(item.get("action") or item.get("command") or "").strip()
                if action_str and (raw_name.endswith(f"({action_str})") or raw_name.endswith(f"（{action_str}）")):
                    clean_name = raw_name[:-len(f"({action_str})")].strip()
                    item["name"] = clean_name

                # 严格匹配已有自定义实现（避免跨服务重名 command 碰撞）
                matched_candidate = (
                    custom_apis_by_id.get(api_id) or
                    custom_apis_by_full_cmd.get(full_cmd) or
                    custom_apis_by_name.get(name)
                )

                if matched_candidate and matched_candidate.api_id not in used_custom_api_ids:
                    matched_custom = matched_candidate
                    used_custom_api_ids.add(matched_custom.api_id)
                    # 同步官方标准目录层级、名称与文档链接
                    matched_custom.name = item.get("name", matched_custom.name)
                    matched_custom.category = item.get("category", matched_custom.category)
                    matched_custom.subcategory = item.get("subcategory", matched_custom.subcategory)
                    matched_custom.path = item.get("path", [])
                    matched_custom.doc_id = doc_id
                    matched_custom.doc_url = item.get("doc_url", matched_custom.doc_url)
                    matched_custom.service_type = item.get("service_type", default_service_type)
                    api_instance = matched_custom
                else:
                    api_instance = DynamicTimApi(item)

                # 全维度多键索引 (支持 Action、command、api_id、全路径及大小写不敏感检索)
                def _bind(key: Optional[str]):
                    if not key:
                        return
                    s = str(key).strip()
                    if not s:
                        return
                    self._apis[s] = api_instance
                    self._apis[s.lower()] = api_instance

                _bind(api_instance.api_id)
                _bind(api_id)
                _bind(api_instance.name)
                if raw_name:
                    _bind(raw_name)
                _bind(getattr(api_instance, "action", None))
                _bind(getattr(api_instance, "command", None))
                _bind(getattr(api_instance, "full_command", None))

                service_name = getattr(api_instance, "service_name", None)
                cmd = getattr(api_instance, "command", None)
                if service_name and cmd:
                    _bind(f"{service_name}/{cmd}")
                    _bind(f"v4/{service_name}/{cmd}")

                act = getattr(api_instance, "action", None) or cmd
                if act:
                    _bind(f"{default_service_type}_{act}")
                    _bind(f"{default_service_type.lower()}_{act.lower()}")

                if doc_id:
                    _bind(f"{default_service_type}_{doc_id}")
                    if default_service_type == "im":
                        _bind(f"tim_{doc_id}")

                ordered_list.append(api_instance)

        # 3. 注册其他未在 catalog 中的遗留类到 lookup map，但不污染官方目录树
        for inst in custom_apis_by_id.values():
            if inst.api_id not in self._apis:
                self._apis[inst.api_id] = inst
                self._apis[inst.api_id.lower()] = inst

        self._ordered_apis = ordered_list
        self._discovered = True

    def get_api(self, api_id: str) -> Optional[BaseTimApi]:
        self.discover()
        if not api_id:
            return None
        clean = str(api_id).strip()
        with self._lock:
            apis = self._apis
        found = apis.get(clean) or apis.get(clean.lower())
        if found:
            return found
        if clean.startswith("v4/"):
            found = apis.get(clean[3:]) or apis.get(clean[3:].lower())
            if found:
                return found
        else:
            found = apis.get(f"v4/{clean}") or apis.get(f"v4/{clean}".lower())
            if found:
                return found
        return None

    def list_apis(self, service_type: Optional[str] = None) -> List[dict]:
        self.discover()
        with self._lock:
            ordered = list(self._ordered_apis)
        if service_type:
            return [api.to_metadata() for api in ordered if getattr(api, "service_type", "im") == service_type]
        return [api.to_metadata() for api in ordered]

    def list_services(self) -> List[dict]:
        """统计并返回已注册的腾讯云产品服务列表（含 IM、云直播及任意未来扩展服务）"""
        self.discover()
        with self._lock:
            ordered = list(self._ordered_apis)
        service_counts = {}
        for api in ordered:
            st = getattr(api, "service_type", "im") or "im"
            service_counts[st] = service_counts.get(st, 0) + 1

        known_defs = {
            "im": {
                "id": "im",
                "name": "即时通信 IM",
                "tag": "TIM",
                "description": "单聊/群聊/全员推送/会话与资料管理等全量 REST API",
                "doc_url": "https://cloud.tencent.com/document/product/269/1520"
            },
            "live": {
                "id": "live",
                "name": "云直播 (CSS)",
                "tag": "CSS",
                "description": "流状态查询/在线流管理/禁推流/断流/转码与录制规则/防盗链鉴权",
                "doc_url": "https://cloud.tencent.com/document/product/267/20455"
            }
        }

        service_defs = []
        for known_id in ("im", "live"):
            if known_id in service_counts:
                item = dict(known_defs[known_id])
                item["api_count"] = service_counts[known_id]
                service_defs.append(item)

        for st, count in service_counts.items():
            if st not in ("im", "live"):
                service_defs.append({
                    "id": st,
                    "name": f"腾讯云 {st.upper()}",
                    "tag": st.upper(),
                    "description": f"腾讯云 {st.upper()} 标准 API 3.0 云产品接口",
                    "api_count": count,
                    "doc_url": f"https://cloud.tencent.com/document/product/{st}"
                })

        return service_defs

    def get_services_summary(self) -> dict:
        """获取当前注册表中所有业务与接口的动态统计摘要 (SSoT 单点真实源，防止提示词与UI硬编码)"""
        self.discover()
        total_apis = len(self._ordered_apis)
        services = self.list_services()
        parts = [f"{s.get('name', s.get('id'))} ({s.get('api_count', 0)} 项)" for s in services]
        service_names = [s.get('name', s.get('id')) for s in services]

        breakdown_text = "、".join(parts) if parts else f"共 {total_apis} 项"
        prompt_text = (
            f"腾讯云多业务全量云接口（动态聚合 {len(services)} 大服务体系、共计 {total_apis} 项官方接口，"
            f"当前已加载 {breakdown_text}，支持即插即用任意扩展更多业务）"
        )

        return {
            "total_apis": total_apis,
            "services_count": len(services),
            "service_names": service_names,
            "breakdown_text": breakdown_text,
            "prompt_text": prompt_text
        }

    def reload(self):
        shadow = ApiRegistry()
        shadow._discover_unlocked()
        with self._lock:
            self._apis = shadow._apis
            self._ordered_apis = shadow._ordered_apis
            self._discovered = True


registry = ApiRegistry()
