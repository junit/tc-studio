# -*- coding: utf-8 -*-
"""
腾讯云直播 (CSS) 专用辅助工具库
提供推流地址生成、播放防盗链鉴权签名 (txSecret & txTime)、流地址反解等常用能力
官方文档参考: https://cloud.tencent.com/document/product/267/32735
"""

import hashlib
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse


def generate_live_push_url(
    domain: str,
    stream_name: str,
    key: Optional[str] = None,
    expire_seconds: int = 86400,
    app_name: str = "live"
) -> dict[str, Any]:
    """
    生成腾讯云直播推流地址 (RTMP / WebRTC / SRT)
    公式: txSecret = MD5(key + stream_name + txTime)
    """
    if not isinstance(domain, str) or not domain.strip():
        raise ValueError("domain is required")
    domain = domain.strip().rstrip("/")
    if domain.startswith("http://") or domain.startswith("https://"):
        domain = domain.split("://", 1)[1]

    query_part = ""
    tx_secret = ""
    tx_time = ""

    if key:
        tx_time_int = int(time.time()) + int(expire_seconds)
        tx_time = hex(tx_time_int)[2:].upper()
        raw_str = f"{key}{stream_name}{tx_time}"
        tx_secret = hashlib.md5(raw_str.encode("utf-8")).hexdigest()
        query_part = f"?txSecret={tx_secret}&txTime={tx_time}"

    rtmp_push_url = f"rtmp://{domain}/{app_name}/{stream_name}{query_part}"
    webrtc_push_url = f"webrtc://{domain}/{app_name}/{stream_name}{query_part}"
    srt_push_url = f"srt://{domain}:9000?streamid=#!::h={domain},r={app_name}/{stream_name}"
    if query_part:
        srt_push_url += f",txSecret={tx_secret},txTime={tx_time}"

    return {
        "stream_name": stream_name,
        "app_name": app_name,
        "domain": domain,
        "txSecret": tx_secret,
        "txTime": tx_time,
        "expire_seconds": expire_seconds,
        "rtmp_push_url": rtmp_push_url,
        "webrtc_push_url": webrtc_push_url,
        "srt_push_url": srt_push_url,
    }


def generate_live_play_urls(
    domain: str,
    stream_name: str,
    key: Optional[str] = None,
    expire_seconds: int = 86400,
    app_name: str = "live",
    use_https: bool = True
) -> dict[str, Any]:
    """
    生成腾讯云直播多协议播放地址 (FLV / HLS / WebRTC / RTMP)
    """
    if not isinstance(domain, str) or not domain.strip():
        raise ValueError("domain is required")
    domain = domain.strip().rstrip("/")
    if domain.startswith("http://") or domain.startswith("https://"):
        domain = domain.split("://", 1)[1]

    scheme = "https" if use_https else "http"
    query_part = ""
    tx_secret = ""
    tx_time = ""

    if key:
        tx_time_int = int(time.time()) + int(expire_seconds)
        tx_time = hex(tx_time_int)[2:].upper()
        raw_str = f"{key}{stream_name}{tx_time}"
        tx_secret = hashlib.md5(raw_str.encode("utf-8")).hexdigest()
        query_part = f"?txSecret={tx_secret}&txTime={tx_time}"

    flv_url = f"{scheme}://{domain}/{app_name}/{stream_name}.flv{query_part}"
    hls_url = f"{scheme}://{domain}/{app_name}/{stream_name}.m3u8{query_part}"
    webrtc_url = f"webrtc://{domain}/{app_name}/{stream_name}{query_part}"
    rtmp_url = f"rtmp://{domain}/{app_name}/{stream_name}{query_part}"

    return {
        "stream_name": stream_name,
        "app_name": app_name,
        "domain": domain,
        "txSecret": tx_secret,
        "txTime": tx_time,
        "expire_seconds": expire_seconds,
        "flv_play_url": flv_url,
        "hls_play_url": hls_url,
        "webrtc_play_url": webrtc_url,
        "rtmp_play_url": rtmp_url,
    }


def parse_live_stream_url(url: str) -> dict:
    """
    反解直播推流或拉流 URL 中的域名、AppName、StreamName 及防盗链签名
    """
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    parts = path.split("/")
    app_name = parts[0] if len(parts) > 0 else "live"
    stream_with_ext = parts[1] if len(parts) > 1 else ""
    stream_name = stream_with_ext.split(".")[0]

    qs = parse_qs(parsed.query)
    tx_secret = qs.get("txSecret", [""])[0]
    tx_time = qs.get("txTime", [""])[0]

    return {
        "scheme": parsed.scheme,
        "domain": parsed.netloc,
        "app_name": app_name,
        "stream_name": stream_name,
        "txSecret": tx_secret,
        "txTime": tx_time
    }
