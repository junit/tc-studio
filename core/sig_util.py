# -*- coding: utf-8 -*-
"""
UserSig (TLSSigAPIv2) 生成与缓存工具
"""

import base64
import hashlib
import hmac
import json
import threading
import time
import zlib
from collections import OrderedDict

_SIG_CACHE_MAX = 1024
_sig_cache: OrderedDict[tuple[int, str, str, int], tuple[str, int, int]] = OrderedDict()
_sig_cache_lock = threading.Lock()


def gen_user_sig(sdkappid: int, key: str, identifier: str, expire: int = 86400 * 7) -> str:
    """生成 UserSig；缓存不保存原始密钥，且按 expire 隔离。"""
    curr_time = int(time.time())
    # A one-way fingerprint prevents the process cache from retaining the secret itself.
    key_fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()
    cache_key = (int(sdkappid), key_fingerprint, str(identifier), int(expire))
    with _sig_cache_lock:
        cached = _sig_cache.get(cache_key)
        if cached:
            cached_sig, gen_time, cached_expire = cached
            if curr_time < gen_time + cached_expire - 3600:
                _sig_cache.move_to_end(cache_key)
                return cached_sig
            _sig_cache.pop(cache_key, None)

    sig_doc = {
        "TLS.ver": "2.0",
        "TLS.identifier": str(identifier),
        "TLS.sdkappid": int(sdkappid),
        "TLS.expire": int(expire),
        "TLS.time": curr_time,
    }
    base_content = (
        f"TLS.identifier:{identifier}\n"
        f"TLS.sdkappid:{sdkappid}\n"
        f"TLS.time:{curr_time}\n"
        f"TLS.expire:{expire}\n"
    )
    sig = hmac.new(key.encode("utf-8"), base_content.encode("utf-8"), hashlib.sha256).digest()
    sig_doc["TLS.sig"] = base64.b64encode(sig).decode("utf-8")

    compressed = zlib.compress(json.dumps(sig_doc).encode("utf-8"))
    base64_str = base64.b64encode(compressed).decode("utf-8")
    user_sig = base64_str.replace("+", "*").replace("/", "-").replace("=", "_")

    with _sig_cache_lock:
        _sig_cache[cache_key] = (user_sig, curr_time, int(expire))
        _sig_cache.move_to_end(cache_key)
        while len(_sig_cache) > _SIG_CACHE_MAX:
            _sig_cache.popitem(last=False)
    return user_sig
