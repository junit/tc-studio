# -*- coding: utf-8 -*-
"""
腾讯云 API 3.0 通用客户端驱动 (TC3-HMAC-SHA256)
支持云直播 (CSS)、实时音视频 (TRTC)、云点播 (VOD)、对象存储 (COS) 等所有遵循 API 3.0 规范的腾讯云服务
"""

import hashlib
import hmac
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from typing import Any, Dict, Optional


class TencentCloudApi3Client:
    def __init__(self, config: Optional[dict] = None):
        self._custom_config = config

    @property
    def config(self) -> dict:
        if self._custom_config is not None:
            return self._custom_config
        from .config import load_config
        return load_config()

    def _get_credentials(self) -> tuple:
        cfg = self.config
        secret_id = cfg.get("tc_secret_id") or cfg.get("secret_id")
        secret_key = cfg.get("tc_secret_key") or cfg.get("secret_key")
        region = cfg.get("tc_region") or cfg.get("region", "ap-guangzhou")
        if not secret_id or not secret_key:
            raise ValueError("腾讯云 API 密钥 (SecretId 或 SecretKey) 未配置，请先在环境配置中填写")
        return secret_id, secret_key, region

    def clean_endpoint(self, endpoint: Optional[str], service: str = "live") -> str:
        """净化 Host 地址：剥除协议头与多余斜杠，避免跨服务端点劫持"""
        if endpoint:
            clean = endpoint
        elif service == "live" and self.config.get("tc_endpoint"):
            clean = self.config.get("tc_endpoint")
        else:
            clean = self.config.get(f"tc_{service}_endpoint") or f"{service}.tencentcloudapi.com"
        return str(clean).replace("https://", "").replace("http://", "").strip().rstrip("/")

    def build_auth_headers(
        self,
        service: str,
        action: str,
        version: str,
        payload: dict,
        endpoint: str,
        region: str,
        secret_id: str,
        secret_key: str,
        timestamp: Optional[int] = None
    ) -> Dict[str, str]:
        """
        按照腾讯云 API 3.0 TC3-HMAC-SHA256 签名规范组装 HTTP 头部
        官方文档参考: https://cloud.tencent.com/document/api/213/30654
        """
        t = timestamp or int(time.time())
        date = time.strftime("%Y-%m-%d", time.gmtime(t))
        host = self.clean_endpoint(endpoint, service=service)
        body_str = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)

        # 1. 拼接规范请求串 CanonicalRequest
        http_request_method = "POST"
        canonical_uri = "/"
        canonical_querystring = ""
        canonical_headers = (
            f"content-type:application/json; charset=utf-8\n"
            f"host:{host}\n"
            f"x-tc-action:{action.lower()}\n"
        )
        signed_headers = "content-type;host;x-tc-action"
        hashed_request_payload = hashlib.sha256(body_str.encode("utf-8")).hexdigest()
        canonical_request = (
            f"{http_request_method}\n"
            f"{canonical_uri}\n"
            f"{canonical_querystring}\n"
            f"{canonical_headers}\n"
            f"{signed_headers}\n"
            f"{hashed_request_payload}"
        )

        # 2. 拼接待签名字符串 StringToSign
        algorithm = "TC3-HMAC-SHA256"
        credential_scope = f"{date}/{service}/tc3_request"
        hashed_canonical_request = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        string_to_sign = f"{algorithm}\n{t}\n{credential_scope}\n{hashed_canonical_request}"

        # 3. 计算签名 Signature
        def _sign(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

        secret_date = _sign(("TC3" + secret_key).encode("utf-8"), date)
        secret_service = _sign(secret_date, service)
        secret_signing = _sign(secret_service, "tc3_request")
        signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        # 4. 组装请求头部字典
        authorization = (
            f"{algorithm} Credential={secret_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Host": host,
            "Authorization": authorization,
            "X-TC-Action": action,
            "X-TC-Version": version,
            "X-TC-Timestamp": str(t),
            "X-TC-Region": region,
        }
        return headers

    def invoke(
        self,
        service: str,
        action: str,
        version: str,
        payload: dict,
        endpoint: Optional[str] = None,
        region: Optional[str] = None,
        timeout: int = 15
    ) -> dict:
        """
        向腾讯云 API 3.0 网关发起统一调用并归一化响应格式
        """
        secret_id, secret_key, default_region = self._get_credentials()
        req_region = region or default_region
        req_endpoint = self.clean_endpoint(endpoint, service=service)

        t_start = time.time()
        try:
            headers = self.build_auth_headers(
                service=service,
                action=action,
                version=version,
                payload=payload,
                endpoint=req_endpoint,
                region=req_region,
                secret_id=secret_id,
                secret_key=secret_key
            )
            body_bytes = json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode("utf-8")
            url = f"https://{req_endpoint}"
            req = Request(url, data=body_bytes, headers=headers, method="POST")

            with urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
                elapsed_ms = int((time.time() - t_start) * 1000)
                parsed = json.loads(body)
                response_data = parsed.get("Response", {})

                # 腾讯云 API 3.0 错误结构: Response.Error = { Code: "...", Message: "..." }
                has_error = "Error" in response_data
                err_code = response_data.get("Error", {}).get("Code", 0) if has_error else 0
                err_info = response_data.get("Error", {}).get("Message", "") if has_error else ""

                return {
                    "ActionStatus": "FAIL" if has_error else "OK",
                    "ErrorCode": err_code,
                    "ErrorInfo": err_info,
                    "Response": response_data,
                    "_cost_ms": elapsed_ms,
                    "_raw": parsed
                }
        except HTTPError as e:
            elapsed_ms = int((time.time() - t_start) * 1000)
            err_body = e.read().decode("utf-8", errors="ignore")
            try:
                parsed_err = json.loads(err_body)
                resp_err = parsed_err.get("Response", {}).get("Error", {})
                code = resp_err.get("Code", e.code)
                msg = resp_err.get("Message", f"HTTP {e.code}: {err_body}")
            except Exception:
                code = e.code
                msg = f"HTTP {e.code}: {err_body}"

            return {
                "ActionStatus": "FAIL",
                "ErrorCode": code,
                "ErrorInfo": msg,
                "_cost_ms": elapsed_ms
            }
        except URLError as e:
            elapsed_ms = int((time.time() - t_start) * 1000)
            return {
                "ActionStatus": "FAIL",
                "ErrorCode": -1,
                "ErrorInfo": f"网络连接失败: {e.reason}",
                "_cost_ms": elapsed_ms
            }
        except Exception as e:
            elapsed_ms = int((time.time() - t_start) * 1000)
            return {
                "ActionStatus": "FAIL",
                "ErrorCode": -2,
                "ErrorInfo": str(e),
                "_cost_ms": elapsed_ms
            }

    def generate_preview(
        self,
        service: str,
        action: str,
        version: str,
        payload: dict,
        endpoint: Optional[str] = None,
        region: Optional[str] = None
    ) -> dict:
        """
        生成预览模式信息 (包括 cURL 命令、请求头及报文)
        """
        secret_id = self.config.get("tc_secret_id") or self.config.get("secret_id") or "AKID_PREVIEW_SECRET_ID"
        secret_key = self.config.get("tc_secret_key") or self.config.get("secret_key") or "PREVIEW_SECRET_KEY"
        req_region = region or self.config.get("tc_region") or "ap-guangzhou"
        req_endpoint = self.clean_endpoint(endpoint, service=service)

        timestamp = int(time.time())
        headers = self.build_auth_headers(
            service=service,
            action=action,
            version=version,
            payload=payload,
            endpoint=req_endpoint,
            region=req_region,
            secret_id=secret_id,
            secret_key=secret_key,
            timestamp=timestamp
        )

        body_str = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
        headers_str = " \\\n  ".join([f'-H "{k}: {v}"' for k, v in headers.items()])
        curl_cmd = (
            f'curl -X POST "https://{req_endpoint}" \\\n'
            f'  {headers_str} \\\n'
            f"  -d '{body_str}'"
        )

        return {
            "endpoint": f"https://{req_endpoint}",
            "headers": headers,
            "curl_command": curl_cmd,
            "payload": payload
        }

    def test_connection(self, service: str = "live", action: Optional[str] = None, version: Optional[str] = None) -> dict:
        """
        通过调用只读查询接口测试 API 密钥与网络连通性
        支持传入指定 service 与 action，若未指定则自动选用该服务的安全探测接口
        """
        secret_id, secret_key, region = self._get_credentials()
        svc = service or "live"
        act = action
        ver = version

        if not act:
            if svc == "live":
                act = "DescribeLiveDomains"
                ver = ver or "2018-08-01"
            elif svc == "trtc":
                act = "DescribeTrtcRoomId"
                ver = ver or "2019-07-22"
            elif svc == "vod":
                act = "DescribeMediaInfos"
                ver = ver or "2018-07-17"
            else:
                act = "DescribeZones"
                svc = "cvm"
                ver = "2017-03-12"

        res = self.invoke(
            service=svc,
            action=act,
            version=ver or "2018-08-01",
            payload={"PageSize": 10, "PageNum": 1} if act == "DescribeLiveDomains" else {},
            region=region
        )
        return res
