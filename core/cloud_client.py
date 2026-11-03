# -*- coding: utf-8 -*-
"""
统一云服务客户端工厂与调度网关 (Universal Cloud Client)
负责根据 API 的服务类型 (IM vs API 3.0 / 云直播等) 智能路由到对应客户端驱动
"""

import json
import random
from typing import Any, Dict, Optional

from .config import load_config
from .tim_client import TimClient
from .tc3_client import TencentCloudApi3Client


class UniversalCloudClient:
    def __init__(self, config: Optional[dict] = None):
        self._custom_config = config
        self.tim_client = TimClient(config)
        self.tc3_client = TencentCloudApi3Client(config)

    @property
    def config(self) -> dict:
        if self._custom_config is not None:
            return self._custom_config
        return load_config()

    def resolve_endpoint(self, service: str, api_meta: Optional[Any] = None) -> str:
        """根据 service 类型与元数据动态计算合法的 endpoint Host (防劫持与协议前缀清理)"""
        meta_ep = getattr(api_meta, "endpoint", None) if api_meta else None
        cfg = self.config
        cfg_ep = cfg.get(f"tc_{service}_endpoint")
        if not cfg_ep and service == "live":
            cfg_ep = cfg.get("tc_endpoint")

        ep = meta_ep or cfg_ep or f"{service}.tencentcloudapi.com"
        return str(ep).replace("https://", "").replace("http://", "").strip().rstrip("/")

    def invoke_api(self, api_meta: Any, payload: dict, timeout: int = 15) -> dict:
        """
        统一调用接口：自动根据 api_meta.service_type 选择相应驱动
        """
        service_type = getattr(api_meta, "service_type", "im") or "im"

        if service_type == "im":
            # 腾讯云 IM 专有体系 (v4 REST + UserSig)
            return self.tim_client.invoke(
                service_name=api_meta.service_name,
                command=api_meta.command,
                payload=payload,
                timeout=timeout
            )
        else:
            # 腾讯云 API 3.0 体系 (云直播 live / TRTC / VOD 等)
            service = getattr(api_meta, "service_name", "") or service_type
            action = getattr(api_meta, "action", "") or api_meta.command
            version = getattr(api_meta, "version", "2018-08-01")
            endpoint = self.resolve_endpoint(service, api_meta)
            region = getattr(api_meta, "region", None) or self.config.get("tc_region", "ap-guangzhou")

            return self.tc3_client.invoke(
                service=service,
                action=action,
                version=version,
                payload=payload,
                endpoint=endpoint,
                region=region,
                timeout=timeout
            )

    def generate_preview(self, api_meta: Any, payload: dict) -> dict:
        """
        统一预览模式生成器 (返回 cURL、请求头与预览地址)
        """
        service_type = getattr(api_meta, "service_type", "im") or "im"

        if service_type == "im":
            sdkappid = self.config.get("sdkappid", 1400000000)
            admin_user = self.config.get("admin_user", "administrator")
            domain = self.config.get("domain", "console.tim.qq.com")
            rand = random.randint(10000000, 99999999)
            usersig = "eJx1j...mock_usersig_for_preview"
            try:
                usersig = self.tim_client.get_usersig()
            except Exception:
                pass

            full_cmd = (
                api_meta.command
                if api_meta.command.startswith("v4/")
                else f"v4/{api_meta.service_name}/{api_meta.command}"
            )
            preview_url = (
                f"https://{domain}/{full_cmd}"
                f"?sdkappid={sdkappid}&identifier={admin_user}&usersig={usersig}&random={rand}&contenttype=json"
            )

            curl_cmd = (
                f'curl -X POST "{preview_url}" \\\n'
                f'  -H "Content-Type: application/json;charset=utf-8" \\\n'
                f"  -d '{json.dumps(payload, ensure_ascii=False, indent=2)}'"
            )

            return {
                "service_type": "im",
                "preview_url": preview_url,
                "curl_command": curl_cmd,
                "headers": {"Content-Type": "application/json;charset=utf-8"},
                "payload": payload
            }
        else:
            service = getattr(api_meta, "service_name", "") or service_type
            action = getattr(api_meta, "action", "") or api_meta.command
            version = getattr(api_meta, "version", "2018-08-01")
            endpoint = self.resolve_endpoint(service, api_meta)
            region = getattr(api_meta, "region", None) or self.config.get("tc_region", "ap-guangzhou")

            tc3_prev = self.tc3_client.generate_preview(
                service=service,
                action=action,
                version=version,
                payload=payload,
                endpoint=endpoint,
                region=region
            )

            return {
                "service_type": service_type,
                "preview_url": tc3_prev["endpoint"],
                "curl_command": tc3_prev["curl_command"],
                "headers": tc3_prev["headers"],
                "payload": payload
            }

    def test_connection(self, service: str = "im") -> dict:
        """根据 service 类型动态路由到相应驱动执行连通性测试"""
        service_norm = str(service or "im").lower()
        if service_norm in ("im", "tim"):
            return self.tim_client.test_connection()
        else:
            return self.tc3_client.test_connection(service=service_norm)
